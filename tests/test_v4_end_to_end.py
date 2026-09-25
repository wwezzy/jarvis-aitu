from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from database.time import aware
from database.v4_models import LmsEvent, NotificationDelivery
from handlers import assistant
from services.action_evidence import supported_actions
from services import notifications, scheduler
from services.preferences import save_preferences
from services.schedule import upsert_schedule_entry, resolve_day
from services.assignments import list_upcoming_assignments


def test_no_invented_deadlines_plans_or_pc_authorization():
    text = 'WEB завтра. OS в воскресенье. SDP вроде на следующей неделе. Завтра зал в 11:00. Хочу жим 100 кг.'
    result = supported_actions({'assignments': [{'title': 'WEB', 'due_at': '2030-01-09 23:59:00'},
        {'title': 'SDP', 'due_at': '2030-01-15 11:00:00', 'deadline_evidence': 'SDP вроде на следующей неделе.'}],
        'workout_log': {'performed_evidence': 'Хочу жим 100 кг.', 'sets': [{}]}, 'system_command': 'shutdown'}, text)
    assert all(x['due_at'] is None for x in result['assignments'])
    assert result['workout_log'] is None and result['system_command'] is None
    result = supported_actions({'assignments': [{'title': 'WEB', 'due_at': '2030-01-09 18:00:00',
        'deadline_evidence': 'WEB завтра в 18:00'}]}, 'WEB завтра в 18:00')
    assert result['assignments'][0]['due_at']


async def test_long_voice_reply_then_evidence_actions_and_one_day_move(db, monkeypatch):
    now = aware(datetime(2030, 1, 8, 8))
    block = await upsert_schedule_entry(42, {'weekday': 2, 'start': '15:00', 'end': '16:30',
        'title': 'Gym', 'block_type': 'planned', 'category': 'training'})
    text = ('WEB завтра. OS в воскресенье. SDP вроде на следующей неделе. Завтра зал в 11:00. ' + 'Контекст учебы. ' * 50)
    status = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(from_user=SimpleNamespace(id=42, full_name='Test'), text=None, caption=None,
        voice=object(), photo=None, document=None, answer=AsyncMock(return_value=status))
    monkeypatch.setattr(assistant, '_download_attachment', AsyncMock(return_value=(text, None, None, None)))
    monkeypatch.setattr(assistant, 'ensure_user', AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, '_local_now', lambda: now)
    monkeypatch.setattr(assistant, 'generate_reply', AsyncMock(return_value='Сначала ответ.'))
    async def extract(**kwargs):
        status.edit_text.assert_awaited_once_with('Сначала ответ.')
        return {'assignments': [{'title': 'WEB', 'course': 'WEB', 'due_at': '2030-01-09 23:59:00'}]}
    monkeypatch.setattr(assistant, 'extract_actions', extract)
    monkeypatch.setattr(assistant, 'sync_schedule_jobs', AsyncMock())
    monkeypatch.setattr(assistant, 'sync_assignment_jobs', AsyncMock())
    await assistant.assistant_message(message, Mock(), Mock())
    task = (await list_upcoming_assignments(42, now=now))[0]
    assert task['due_at'] is None
    assert (await resolve_day(42, now.date() + timedelta(days=1)))[0]['start'] == '11:00'
    assert (await resolve_day(42, now.date() + timedelta(days=8)))[0]['start'] == '15:00'
    assert block['id']


async def test_optional_quiz_open_never_becomes_deadline_and_dnd_queues(db, monkeypatch):
    now = datetime.now(scheduler.settings.timezone)
    async with db() as session:
        session.add(LmsEvent(user_id=42, source='lms_ical', external_uid='quiz-open-test',
            title='Quiz opens', event_type='quiz_open', starts_at=now - timedelta(minutes=1), status='active', raw_hash='0' * 64))
        await session.commit()
    bot = SimpleNamespace(send_message=AsyncMock())
    await scheduler.send_quiz_open_notices(bot, 42)
    bot.send_message.assert_not_awaited()
    await save_preferences(42, {'quiz_open_notices': True})
    monkeypatch.setattr(notifications, 'is_dnd', AsyncMock(return_value=True))
    await scheduler.send_quiz_open_notices(bot, 42)
    assert await list_upcoming_assignments(42) == []
    async with db() as session:
        notice = (await session.scalars(select(NotificationDelivery))).one()
        assert notice.status == 'queued' and not notice.critical
    monkeypatch.setattr(notifications, 'is_dnd', AsyncMock(return_value=False))
    assert await notifications.flush_queued(bot, 42) == 1
    assert await notifications.flush_queued(bot, 42) == 0


@pytest.mark.parametrize('category', ['deep_work', 'training', 'study'])
def test_flexible_blocks_never_notify(category):
    assert scheduler._smart_schedule_lead(SimpleNamespace(block_type='flex', category=category, notify_before_min=60)) is None
