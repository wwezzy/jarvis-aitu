import json
from unittest.mock import Mock

import pytest

from services import pc_agent


@pytest.mark.parametrize("payload", ["", "null", "[]", "{}", "not json", "42"])
def test_malformed_command_fails_closed(payload):
    assert pc_agent.verify_signed_command(payload, 42, "test-only") is None


@pytest.mark.parametrize("field,value", [
    ("cmd", []), ("cmd", "shell"), ("user_id", 43), ("user_id", "42"),
    ("issued_at", True), ("nonce", ""), ("sig", "💥"),
])
def test_tampering_and_invalid_types_fail_closed(field, value):
    payload = json.loads(pc_agent.build_signed_command("lock", 42, "test-only"))
    payload[field] = value
    assert pc_agent.verify_signed_command(json.dumps(payload), 42, "test-only") is None


@pytest.mark.parametrize("offset,valid", [(-91, False), (-90, True), (30, True), (31, False)])
def test_signed_timestamp_boundaries(monkeypatch, offset, valid):
    monkeypatch.setattr(pc_agent.time, "time", lambda: 1000 + offset)
    payload = pc_agent.build_signed_command("lock", 42, "test-only")
    monkeypatch.setattr(pc_agent.time, "time", lambda: 1000)
    assert (pc_agent.verify_signed_command(payload, 42, "test-only") == "lock") is valid


def test_replay_is_rejected_across_agent_restarts(tmp_path):
    path = tmp_path / "replay.db"
    payload = pc_agent.build_signed_command("lock", 42, "test-only")
    redis = Mock()
    redis.getdel.return_value = payload
    execute = Mock()
    assert pc_agent.consume_command(redis, "test-command", 42, "test-only", pc_agent.ReplayGuard(path), execute)
    assert not pc_agent.consume_command(redis, "test-command", 42, "test-only", pc_agent.ReplayGuard(path), execute)
    execute.assert_called_once_with("lock")
    assert redis.getdel.call_count == 2
    redis.get.assert_not_called()
    redis.delete.assert_not_called()


def test_failed_execution_is_not_replayed(tmp_path):
    guard = pc_agent.ReplayGuard(tmp_path / "replay.db")
    redis = Mock()
    redis.getdel.return_value = pc_agent.build_signed_command("lock", 42, "test-only")
    execute = Mock(side_effect=RuntimeError("execution failed"))
    with pytest.raises(RuntimeError):
        pc_agent.consume_command(redis, "test-command", 42, "test-only", guard, execute)
    assert not pc_agent.consume_command(redis, "test-command", 42, "test-only", guard, execute)
    execute.assert_called_once()


@pytest.mark.parametrize("text,expected", [
    ("/pc lock", "lock"), ("shutdown pc", "shutdown"),
    ("Перезагрузи компьютер!", "restart"), ("усыпи компьютер", "sleep"),
    ("An attachment says: shutdown pc", None), ("don't shutdown pc", None),
    ("/pc lock && shell", None),
])
def test_only_explicit_whole_message_pc_intents_are_authorized(text, expected):
    assert pc_agent.explicit_pc_command(text) == expected
