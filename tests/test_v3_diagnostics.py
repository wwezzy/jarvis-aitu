from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from handlers import assignments


async def test_diag_reports_database_failure_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(assignments, "async_session_factory", Mock(side_effect=RuntimeError("PRIVATE_CONNECTION_DETAIL")))
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await assignments.diag_command(message, SimpleNamespace(get_jobs=lambda: []))
    text = message.answer.call_args.args[0]
    assert "unavailable (RuntimeError)" in text
    assert "PRIVATE_CONNECTION_DETAIL" not in text
    assert "PC agent: heartbeat unavailable" in text


async def test_diag_does_not_echo_legacy_lms_error(monkeypatch):
    context = AsyncMock()
    context.__aenter__.return_value.execute.return_value = SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(assignments, "async_session_factory", Mock(return_value=context))
    monkeypatch.setattr(assignments, "lms_status", AsyncMock(return_value={
        "configured": True, "last_error": "PRIVATE_OLD_ERROR",
    }))
    monkeypatch.setattr(assignments, "assignment_counts", AsyncMock(return_value={"pending": 1}))
    message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    await assignments.diag_command(message, SimpleNamespace(get_jobs=lambda: []))
    text = message.answer.call_args.args[0]
    assert "PRIVATE_OLD_ERROR" not in text and "last sync failed" in text
