import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import get_settings
from database.models import Assignment, ScheduleEntry
from handlers import assistant
from services import llm
from services.direct_intents import try_direct_answer
from services.memory import build_memory_context


def test_boot_without_any_ai_credentials(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("GEMINI_", "OPENAI_", "NVIDIA_")):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    try:
        assert get_settings().gemini_api_keys == ()
    finally:
        get_settings.cache_clear()


@pytest.fixture
def message():
    status = SimpleNamespace(edit_text=AsyncMock())
    return SimpleNamespace(
        from_user=SimpleNamespace(id=42, full_name="Test"), text="Hi",
        photo=None, document=None, voice=None,
        answer=AsyncMock(return_value=status), status=status,
    )


@pytest.mark.parametrize("query,expected", [
    ("Что сейчас?", "Class A"), ("Что у меня сейчас?", "Class A"),
    ("Что дальше?", "Class A"), ("Что завтра?", "Gym B"),
    ("Какие дедлайны?", "Assignment C"),
])
async def test_deterministic_intents_use_database_with_ai_down(db, monkeypatch, query, expected):
    now = datetime(2030, 1, 7, 10, 15, tzinfo=get_settings().timezone)
    async with db() as session:
        session.add_all([
            ScheduleEntry(user_id=42, weekday=0, start="10:00", end="11:00", title="Class A", category="study", block_type="fixed"),
            ScheduleEntry(user_id=42, weekday=1, start="10:00", end="11:00", title="Gym B", category="training"),
            Assignment(user_id=42, title="Assignment C", due_at=datetime(2030, 1, 8, 12)),
        ])
        await session.commit()
    request = AsyncMock(side_effect=AssertionError("AI must not be used"))
    monkeypatch.setattr(llm, "_openai_reply", request)
    assert expected in await try_direct_answer(42, query, now)
    request.assert_not_awaited()


async def test_handler_short_circuits_ai_for_schedule(db, monkeypatch, message):
    message.text = "Что сейчас?"
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    parse = AsyncMock(side_effect=AssertionError("AI must not be used"))
    monkeypatch.setattr(assistant, "generate_reply", parse)
    await assistant.assistant_message(message, Mock(), Mock())
    parse.assert_not_awaited()
    assert "Сейчас" in message.status.edit_text.call_args.args[0]


async def test_action_write_failure_does_not_overwrite_chat(monkeypatch, message):
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value=""))

    monkeypatch.setattr(assistant, "generate_reply", AsyncMock(return_value="Visible answer"))
    monkeypatch.setattr(assistant, "extract_actions", AsyncMock(return_value={"memory_updates": [{"key": "test", "value": "test"}]}))
    monkeypatch.setattr(assistant, "upsert_memory_updates", AsyncMock(side_effect=RuntimeError("DB offline")))
    await assistant.assistant_message(message, Mock(), Mock())
    message.status.edit_text.assert_awaited_once_with("Visible answer")
    assert "Не удалось сохранить" in message.answer.call_args.args[0]


@pytest.mark.parametrize("payload", ["not json", '{"system_command":"erase"}'])
async def test_schema_failure_never_suppresses_normal_reply(monkeypatch, message, payload):
    from services.schemas import JarvisActions

    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value=""))
    monkeypatch.setattr(assistant, "generate_reply", AsyncMock(return_value="Normal reply"))

    async def extract(**kwargs):
        message.status.edit_text.assert_awaited_once_with("Normal reply")
        return JarvisActions.model_validate_json(payload).model_dump()

    monkeypatch.setattr(assistant, "extract_actions", extract)
    await assistant.assistant_message(message, Mock(), Mock())
    message.status.edit_text.assert_awaited_once_with("Normal reply")


async def test_reply_is_visible_while_extraction_is_blocked(monkeypatch, message):
    extracting, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value=""))
    monkeypatch.setattr(assistant, "generate_reply", AsyncMock(return_value="Normal reply"))

    async def extract(**kwargs):
        extracting.set()
        await release.wait()
        raise TimeoutError

    monkeypatch.setattr(assistant, "extract_actions", extract)
    task = asyncio.create_task(assistant.assistant_message(message, Mock(), Mock()))
    try:
        await asyncio.wait_for(extracting.wait(), 1)
        message.status.edit_text.assert_awaited_once_with("Normal reply")
        assert not task.done()
    finally:
        release.set()
        await task
    message.status.edit_text.assert_awaited_once_with("Normal reply")


async def test_planning_context_contains_seven_days_and_deadlines(db):
    async with db() as session:
        session.add_all([
            ScheduleEntry(user_id=42, weekday=0, start="10:00", end="11:00", title="Required class"),
            Assignment(user_id=42, title="Required deadline", due_at=datetime(2030, 1, 9)),
            Assignment(user_id=42, title="Completed hidden", status="done"),
            Assignment(user_id=43, title="Other user hidden"),
        ])
        await session.commit()
    context = await build_memory_context(42, "Составь план на 7 дней", datetime(2030, 1, 7))
    assert "Required class" in context and "Required deadline" in context
    assert "2030-01-07" in context and "2030-01-13" in context
    assert "Completed hidden" not in context and "Other user hidden" not in context


async def test_tomorrow_uses_local_day_at_utc_boundary(db):
    async with db() as session:
        session.add(ScheduleEntry(user_id=42, weekday=1, start="10:00", end="11:00", title="Tuesday class"))
        await session.commit()
    # Sunday UTC is already Monday in configured UTC+5 timezone.
    answer = await try_direct_answer(42, "Что завтра?", datetime(2030, 1, 6, 21, tzinfo=timezone.utc))
    assert "Tuesday class" in answer


async def test_model_pc_output_cannot_queue_commands(monkeypatch, message):
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock(return_value=SimpleNamespace(preferences=None)))
    monkeypatch.setattr(assistant, "try_direct_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(assistant, "build_memory_context", AsyncMock(return_value=""))

    monkeypatch.setattr(assistant, "generate_reply", AsyncMock(return_value="Visible answer"))
    monkeypatch.setattr(assistant, "extract_actions", AsyncMock(return_value={"system_command": "shutdown"}))
    redis = SimpleNamespace(set=AsyncMock(), get=AsyncMock(return_value=None))
    await assistant.assistant_message(message, Mock(), Mock(), redis)
    redis.set.assert_not_awaited()


async def test_explicit_pc_command_works_without_ai(monkeypatch, message):
    from dataclasses import replace
    from services.pc_agent import verify_signed_command

    message.text = "/pc lock"
    monkeypatch.setattr(assistant, "settings", replace(assistant.settings, pc_agent_secret="test-only"))
    monkeypatch.setattr(assistant, "ensure_user", AsyncMock())
    parse = AsyncMock(side_effect=AssertionError("No AI for PC commands"))
    monkeypatch.setattr(assistant, "generate_reply", parse)
    redis = SimpleNamespace(set=AsyncMock(return_value=True), get=AsyncMock(return_value=None))
    await assistant.assistant_message(message, Mock(), Mock(), redis)
    parse.assert_not_awaited()
    assert verify_signed_command(redis.set.call_args.args[1], 42, "test-only") == "lock"


async def test_memory_context_excludes_unrelated_facts(db):
    from database.models import MemoryFact

    async with db() as session:
        session.add_all([
            MemoryFact(user_id=42, key="algorithms", value="Prefer practice", importance=5),
            MemoryFact(user_id=42, key="unrelated", value="Unrelated private detail", importance=10),
        ])
        await session.commit()
    context = await build_memory_context(42, "algorithms", datetime(2030, 1, 7))
    assert "Prefer practice" in context and "Unrelated private detail" not in context
