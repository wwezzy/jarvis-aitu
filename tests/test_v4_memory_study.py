from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from database.models import MemoryFact
from database.time import aware
from database.v4_models import StudySession
from handlers import assistant
from services.memory import build_memory_context, correct_memory, delete_memory, list_memory_facts, upsert_memory_updates
from services.progression import double_progression_recommendation
from services.semantic import semantic_ids
from services.study import finish_timer, review_topic, save_topic, start_timer, study_context
from test_v3_acceptance import message  # noqa: F401

NOW = aware(datetime(2030, 1, 7, 12))


async def test_memory_correction_expiry_confidence_and_deletion(db):
    await upsert_memory_updates(42, [{"key": "algorithms", "value": "Prefer lectures", "confidence": 0.4},
        {"key": "expired_algorithms", "value": "Old detail", "expires_at": (NOW - timedelta(days=1)).isoformat()}])
    memories = await list_memory_facts(42)
    row = next(m for m in memories if m["key"] == "algorithms")
    await correct_memory(42, row["id"], "Prefer exercises")
    with pytest.raises(ValueError):
        await correct_memory(43, row["id"], "Other user's edit")
    context = await build_memory_context(42, "algorithms", NOW)
    assert "Prefer exercises" in context and "Prefer lectures" not in context and "Old detail" not in context
    async with db() as session:
        stored = await session.get(MemoryFact, row["id"])
        assert stored.confidence == 1 and stored.last_used_at is not None
    await delete_memory(42, row["id"])
    assert "Prefer exercises" not in await build_memory_context(42, "algorithms", NOW)


async def test_semantic_extension_or_provider_failure_falls_back(monkeypatch):
    monkeypatch.setenv("ENABLE_SEMANTIC_MEMORY", "true")
    ranker = SimpleNamespace(rank=AsyncMock(side_effect=RuntimeError("unavailable")))
    assert await semantic_ids("query", [{"id": 1}], ranker) == []
    ranker.rank.side_effect = None
    ranker.rank.return_value = [1]
    assert await semantic_ids("query", [{"id": 1}], ranker) == [1]


async def test_study_timer_does_not_invent_completion_or_mastery(db):
    session_id = await start_timer(42, 25, "OS processes", NOW)
    async with db() as session:
        row = await session.get(StudySession, session_id)
        assert row.completed_at is None and row.reported_minutes is None
    await finish_timer(42, session_id, 12, NOW + timedelta(minutes=25))
    topic = await save_topic(42, "OS", "Processes")
    assert (await study_context(42))[0]["self_reported_score"] is None
    await review_topic(42, topic, 1, NOW)
    first = (await study_context(42))[0]
    assert first["reviews"] == 0 and datetime.fromisoformat(first["next_review_at"]) == NOW + timedelta(days=1)


def test_progression_needs_rir_and_respects_bad_form():
    assert double_progression_recommendation([{"reps": 12}])["action"] != "increase_load"
    assert double_progression_recommendation([{"reps": 12, "rir": 3, "technique_ok": False}])["action"] == "hold_or_reduce"


async def test_voice_model_transcription_never_authorizes_pc(monkeypatch, message):  # noqa: F811
    message.text = None
    message.voice = SimpleNamespace(file_size=10)
    monkeypatch.setattr(assistant, "_download_attachment", AsyncMock(return_value=("/pc sleep", None, None, None)))
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value=""))
    monkeypatch.setattr(assistant, "generate_reply", AsyncMock(return_value="Use an explicit PC command"))
    monkeypatch.setattr(assistant, "extract_actions", AsyncMock(return_value={}))
    redis = SimpleNamespace(set=AsyncMock())
    await assistant.assistant_message(message, Mock(), Mock(), redis)
    redis.set.assert_not_awaited()
