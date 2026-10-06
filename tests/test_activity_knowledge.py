import json
import time
import os
import subprocess
import sys
from datetime import datetime
from unittest.mock import Mock

import fakeredis
import fakeredis.aioredis
import pytest
from sqlalchemy import select

from database.assistant_models import ActivityReceipt
from database.time import aware
from services.activity import ActivityJournal, ingest_activity
from services.autonomy import activity_summary
from services.knowledge import capture_note, list_notes, update_note, knowledge_context
from services.pc_agent import signed_status, read_status
from services.preferences import save_preferences
from services.redis_backend import NativeAsyncRedis


def test_foreground_only_idle_and_clock_gaps_are_not_fabricated(tmp_path):
    sample = Mock(return_value={'process': 'cs2.exe', 'idle_seconds': 0, 'available': True})
    journal = ActivityJournal(tmp_path / 'activity.db', sample=sample)
    journal.tick(120)
    journal.tick(125)
    sample.return_value = {'process': 'chrome.exe', 'idle_seconds': 0, 'available': True}
    journal.tick(130)
    journal.tick(135)
    sample.return_value = {'process': 'cs2.exe', 'idle_seconds': 400, 'available': True}
    journal.tick(140)
    journal.tick(145)
    journal.tick(900)  # sleep/offline gap is excluded
    rows = journal.pending(960)
    totals = {key: sum(r['seconds'].get(key, 0) for r in rows) for key in ['gaming', 'unknown', 'idle']}
    assert totals == {'gaming': 10, 'unknown': 10, 'idle': 5}
    assert 'chrome.exe' not in json.dumps(rows) and 'cs2.exe' not in json.dumps(rows)
    assert sum(totals.values()) == 25
    journal.acknowledge([r['id'] for r in rows])
    assert journal.pending(960) == []
    journal.clear()


async def test_signed_activity_delivery_restart_ack_idempotency_and_opt_out(db, tmp_path):
    now = int(time.time())
    minute = now // 60 * 60 - 120
    def sample():
        return {'process': 'code.exe', 'idle_seconds': 0, 'available': True}
    journal = ActivityJournal(tmp_path / 'activity.db', sample=sample)
    journal.tick(minute)
    journal.tick(minute + 5)
    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(server=server, decode_responses=True))
    journal.sync(sync, 42, 'test-only')
    assert await ingest_activity(redis, 42, 'test-only') == 0
    await save_preferences(42, {'activity_enabled': True})
    assert await ingest_activity(redis, 42, 'wrong-secret') == 0
    assert await ingest_activity(redis, 42, 'test-only') == 1
    assert await ingest_activity(redis, 42, 'test-only') == 1
    async with db() as session:
        assert len(list(await session.scalars(select(ActivityReceipt)))) == 1
    summary = await activity_summary(42, aware(datetime.now()))
    assert summary['seconds']['work'] == 5
    restarted = ActivityJournal(tmp_path / 'activity.db', sample=sample)
    restarted.sync(sync, 42, 'test-only')
    assert restarted.pending() == []
    assert read_status(sync.get('jarvis:activity:42'), 42, 'test-only')['buckets'] == []


@pytest.mark.parametrize('seconds', [{'gaming': 61}, {'gaming': 50, 'work': 50}, {'gaming': True}, {'SECRET_WINDOW': 3}, {}])
async def test_signed_but_impossible_activity_is_rejected(db, seconds):
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await save_preferences(42, {'activity_enabled': True})
    now = int(time.time())
    await redis.set('jarvis:activity:42', signed_status({'user_id': 42, 'seen_at': now,
        'buckets': [{'id': 'f' * 32, 'starts': now // 60 * 60 - 60, 'seconds': seconds}]}, 'test-only'))
    assert await ingest_activity(redis, 42, 'test-only') == 0


async def test_capture_source_recall_delete_and_user_isolation(db):
    text = 'Research source: retrieval practice means recalling what you studied.'
    note = await capture_note(42, text, tags=['study'])
    assert (await capture_note(42, text))['id'] == note['id']
    assert len(await list_notes(42)) == 1
    assert await list_notes(43) == []
    assert str(note['id']) in await knowledge_context(42, 'retrieval practice')
    with pytest.raises(ValueError):
        await update_note(43, note['id'], 'reference')
    await update_note(42, note['id'], 'archived')
    assert await knowledge_context(42, 'retrieval practice') == ''
    await update_note(42, note['id'], remove=True)
    assert await list_notes(42) == []


@pytest.mark.parametrize('text', ['API_KEY=sk-' + 'a' * 30, 'REDIS_URL=rediss://test:password@localhost', 'BOT_TOKEN=123456789:' + 'x' * 35])
async def test_secret_config_is_not_captured(db, text):
    with pytest.raises(ValueError, match='секрет'):
        await capture_note(42, text)
    assert await list_notes(42) == []


async def test_search_wildcards_are_literal(db):
    await capture_note(42, '100 percent complete')
    assert await list_notes(42, '%') == []
    assert await list_notes(42, '_') == []
    assert await list_notes(42, 'percent')


async def test_large_material_keeps_every_character_and_capture_can_be_disabled(db):
    from services.knowledge import capture_material
    text = ('Known source material. ' * 2100).strip()
    notes = await capture_material(42, text, 'test_source')
    assert len(notes) == 3
    assert ''.join(n['content'] for n in notes).replace(' ', '') == text.replace(' ', '')
    assert [n['id'] for n in await capture_material(42, text, 'test_source')] == [n['id'] for n in notes]
    await save_preferences(42, {'auto_capture_materials': False})
    assert await capture_material(42, 'New source', 'test_source') == []


def test_local_monitor_needs_no_bot_token_or_database(tmp_path):
    environment = {key: value for key, value in os.environ.items() if key not in {'BOT_TOKEN', 'ADMIN_ID', 'DATABASE_URL'}}
    environment['PYTHON_DOTENV_DISABLED'] = '1'
    code = """
import tempfile
from pathlib import Path
from services.activity import ActivityJournal
with tempfile.TemporaryDirectory() as folder:
    journal=ActivityJournal(Path(folder)/'activity.db', sample=lambda: {'process':'code.exe','idle_seconds':0,'available':True})
    journal.tick(120)
    journal.tick(125)
    assert journal.pending(180)[0]['seconds']=={'work':5}
print('standalone monitor ok')
"""
    result = subprocess.run([sys.executable, '-c', code], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'standalone monitor ok'


async def test_deleted_server_activity_does_not_reappear_from_spool(db):
    from services.autonomy import autonomy_command
    now = aware(datetime.now())
    redis = NativeAsyncRedis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await save_preferences(42, {'activity_enabled': True})
    start = int(now.timestamp()) // 60 * 60 - 60
    data = {'user_id': 42, 'seen_at': int(now.timestamp()), 'buckets': [
        {'id': 'd' * 32, 'starts': start, 'seconds': {'gaming': 30}}]}
    await redis.set('jarvis:activity:42', signed_status(data, 'test-only'))
    assert await ingest_activity(redis, 42, 'test-only', now) == 1
    await autonomy_command(42, '/activity delete', now)
    assert await ingest_activity(redis, 42, 'test-only', now) == 0
    assert 'd' * 32 in read_status(await redis.get('jarvis:activity_ack:42'), 42, 'test-only')['ids']
    async with db() as session:
        assert list(await session.scalars(select(ActivityReceipt))) == []
