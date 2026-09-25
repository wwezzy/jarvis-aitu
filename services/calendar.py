"""Opt-in Google Calendar adapter; writes only explicitly created Jarvis events."""
import hashlib
import os
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from sqlalchemy import delete, select

from config import get_settings
from database import engine
from database.time import aware
from database.v4_models import PersonalCalendarEvent


def configured():
    return os.getenv('ENABLE_GOOGLE_CALENDAR', '').lower() == 'true' and all(os.getenv(k) for k in (
        'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REFRESH_TOKEN', 'GOOGLE_CALENDAR_ID'))


class CalendarUnavailable(RuntimeError):
    pass


class GoogleCalendar:
    def __init__(self):
        self.base = 'https://www.googleapis.com/calendar/v3/calendars/' + quote(os.getenv('GOOGLE_CALENDAR_ID', ''), safe='') + '/events'

    async def token(self):
        if not configured():
            raise CalendarUnavailable('Google Calendar is disabled or unconfigured')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post('https://oauth2.googleapis.com/token', data={
                'client_id': os.environ['GOOGLE_CLIENT_ID'], 'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
                'refresh_token': os.environ['GOOGLE_REFRESH_TOKEN'], 'grant_type': 'refresh_token'}) as response:
                if response.status != 200:
                    raise CalendarUnavailable('Google OAuth refresh failed')
                data = await response.json()
                return data['access_token']

    async def request(self, method, token, identity=None, *, params=None, payload=None):
        url = self.base + ('/' + quote(identity, safe='') if identity else '')
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.request(method, url, headers={'Authorization': 'Bearer ' + token}, params=params, json=payload) as response:
                if response.status not in {200, 201}:
                    raise CalendarUnavailable(f'Google Calendar HTTP {response.status}')
                return await response.json()

    async def events(self, start, end):
        token, page, events = await self.token(), None, []
        for _ in range(10):
            params = {'timeMin': start.isoformat(), 'timeMax': end.isoformat(), 'singleEvents': 'true',
                      'showDeleted': 'false', 'maxResults': '500', 'timeZone': str(get_settings().timezone)}
            if page:
                params['pageToken'] = page
            data = await self.request('GET', token, params=params)
            events.extend(data.get('items', []))
            page = data.get('nextPageToken')
            if not page:
                return events
        raise CalendarUnavailable('Google Calendar snapshot exceeds 5000 events; nothing changed')


def _stamp(value):
    if value.get('dateTime'):
        result = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(value.get('timeZone') or str(get_settings().timezone)))
        return aware(result)
    return aware(datetime.combine(datetime.fromisoformat(value['date']).date(), time.min))


def serialize(row):
    return {'id': row.external_id, 'title': row.title, 'start': row.starts_at.isoformat(),
            'end': row.ends_at.isoformat(), 'jarvis_owned': row.jarvis_owned}


async def list_events(user_id):
    async with engine.async_session_factory() as db:
        rows = await db.scalars(select(PersonalCalendarEvent).where(PersonalCalendarEvent.user_id == user_id)
                               .order_by(PersonalCalendarEvent.starts_at).limit(5000))
        return [serialize(row) for row in rows]


async def sync_calendar(user_id, *, adapter=None, now=None):
    if not configured():
        return {'configured': False, 'events': 0}
    now = aware(now or datetime.now(get_settings().timezone))
    left, right = now - timedelta(days=7), now + timedelta(days=90)
    items = await (adapter or GoogleCalendar()).events(left, right)
    parsed = []
    for item in items:
        if item.get('status') == 'cancelled' or item.get('transparency') == 'transparent':
            continue
        start, end = _stamp(item['start']), _stamp(item['end'])
        if end <= start:
            raise CalendarUnavailable('Invalid calendar interval; snapshot not applied')
        parsed.append(dict(external_id=item['id'], title=str(item.get('summary') or 'Busy')[:255], starts_at=start, ends_at=end,
            jarvis_owned=item.get('extendedProperties', {}).get('private', {}).get('jarvis_owner') == str(user_id)))
    async with engine.async_session_factory() as db:
        # Reconcile only after the whole snapshot is fetched; keep stable IDs.
        await db.execute(delete(PersonalCalendarEvent).where(PersonalCalendarEvent.user_id == user_id,
            PersonalCalendarEvent.starts_at < right, PersonalCalendarEvent.ends_at > left,
            PersonalCalendarEvent.external_id.not_in([item['external_id'] for item in parsed])))
        for item in parsed:
            existing = await db.scalar(select(PersonalCalendarEvent).where(PersonalCalendarEvent.user_id == user_id,
                PersonalCalendarEvent.external_id == item['external_id']))
            if existing:
                for key, value in item.items():
                    setattr(existing, key, value)
            else:
                db.add(PersonalCalendarEvent(user_id=user_id, **item))
        await db.commit()
    return {'configured': True, 'events': len(parsed)}


async def conflicts(user_id, start, end, *, exclude=None):
    from services.schedule import resolve_day
    found = []
    day = start.date()
    while day <= end.date():
        for block in await resolve_day(user_id, day):
            if block.get('block_type') == 'flex' or (exclude and block.get('external_id') == exclude):
                continue
            a = aware(datetime.combine(day, time.fromisoformat(block['start'])))
            b = aware(datetime.combine(day, time.fromisoformat(block['end'])))
            if b <= a:
                b += timedelta(days=1)
            if a < end and b > start:
                found.append({'date': day.isoformat(), 'title': block['title'], 'start': block['start'], 'end': block['end']})
        day += timedelta(days=1)
    return found


async def save_event(user_id, payload, *, adapter=None):
    if not configured() or os.getenv('ENABLE_GOOGLE_CALENDAR_WRITE', '').lower() != 'true':
        raise CalendarUnavailable('Google Calendar writes are disabled')
    start, end = aware(datetime.fromisoformat(payload['start'])), aware(datetime.fromisoformat(payload['end']))
    title = str(payload['title']).strip()
    if not title or len(title) > 255 or not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError('Invalid calendar event')
    adapter = adapter or GoogleCalendar()
    token = await adapter.token()
    identity = payload.get('id')
    marker = {'jarvis_owner': str(user_id)}
    if identity:
        existing = await adapter.request('GET', token, identity)
        if existing.get('extendedProperties', {}).get('private', {}).get('jarvis_owner') != str(user_id):
            raise ValueError('Only Jarvis-owned events can be edited')
    else:
        operation = str(uuid.UUID(payload['operation_id']))
        identity = hashlib.sha256(f'{user_id}:{operation}'.encode()).hexdigest()[:32]
    collisions = await conflicts(user_id, start, end, exclude=identity)
    if collisions and payload.get('allow_conflicts') is not True:
        return {'saved': False, 'conflicts': collisions}
    body = {'summary': title, 'start': {'dateTime': start.isoformat()}, 'end': {'dateTime': end.isoformat()},
            'extendedProperties': {'private': marker}}
    if not payload.get('id'):
        body['id'] = identity
    # Deterministic create ID makes retries safe: a conflict is reported, never
    # retried with a fresh ID or allowed to modify an unrelated event.
    result = await adapter.request('PATCH' if payload.get('id') else 'POST', token,
        identity if payload.get('id') else None, params={'sendUpdates': 'none'}, payload=body)
    async with engine.async_session_factory() as db:
        row = await db.scalar(select(PersonalCalendarEvent).where(PersonalCalendarEvent.user_id == user_id,
            PersonalCalendarEvent.external_id == result['id']))
        if row is None:
            row = PersonalCalendarEvent(user_id=user_id, external_id=result['id'])
            db.add(row)
        row.title, row.starts_at, row.ends_at, row.jarvis_owned = title, start, end, True
        await db.commit()
    return {'saved': True, 'id': result['id'], 'conflicts': collisions}
