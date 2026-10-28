"""Owner consent and finite gesture authority, without OS or camera access."""
from types import SimpleNamespace

import pytest

from desktop.actions import ALLOWED_ACTIONS
from desktop.core import DesktopCore
from desktop.owner_auth import AuthResult, OwnerSession


@pytest.fixture
def controlled(tmp_path):
    actions = []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Выполнено")
    clock = SimpleNamespace(now=100.0)
    verifier = SimpleNamespace(verify=lambda *_args, **_kwargs: AuthResult(True, "verified", "Проверено"))
    core.owner = OwnerSession(True, ttl_seconds=10, clock=lambda: clock.now,
                              verifier=verifier, session_check=lambda: True)
    core.store.set_preferences({"voice_owner_required": True, "gestures_enabled": True})
    yield core, clock, actions
    core.close()


@pytest.mark.parametrize("source", ["typed", "voice"])
@pytest.mark.parametrize("text", ["открой блокнот", "добавь задачу Секрет", "как дела", "объясни фотосинтез"])
def test_owner_is_checked_before_history_or_action(controlled, source, text):
    core, _, actions = controlled
    before = core.store.snapshot()
    reply = core.handle(text, source=source)
    assert "Windows Hello" in reply.text
    assert core.store.snapshot() == before
    assert actions == []


def test_disabling_enabled_owner_policy_requires_fresh_consent(controlled):
    core, _, _ = controlled
    assert not core.set_owner_required(False)
    assert core.owner.enabled
    assert core.owner.verify(123).success
    assert core.set_owner_required(False)
    assert not core.owner.enabled
    assert not core.store.preferences()["gestures_enabled"]


@pytest.mark.parametrize("action", sorted(ALLOWED_ACTIONS))
def test_gestures_have_exactly_three_non_power_actions(controlled, action):
    core, _, actions = controlled
    assert core.owner.verify(123).success
    reply = core.request_control(action, source="gesture")
    if action in {"volume_up", "volume_down", "minimize"}:
        assert actions == [action]
    else:
        assert not actions
    assert reply.confirmation_id is None


@pytest.mark.parametrize("change", ["disabled_policy", "disabled_gestures", "expired", "revoked", "session_lock"])
def test_queued_gesture_is_rechecked_at_core_dispatch(controlled, change):
    core, clock, actions = controlled
    assert core.owner.verify(123).success
    if change == "disabled_policy":
        core.owner.set_enabled(False)
        assert core.owner.allowed()  # Disabled does not grant gesture authority.
    elif change == "disabled_gestures":
        core.store.set_preferences({"gestures_enabled": False})
    elif change == "expired":
        clock.now += 10
    elif change == "revoked":
        core.owner.revoke()
    else:
        core.owner.handle_session_change(7)
    core.request_control("volume_up", source="gesture")
    assert not actions


@pytest.mark.parametrize("bad", [None, [], {}, 42, True, "", "shutdown; whoami"])
def test_malformed_control_actions_fail_closed(controlled, bad):
    core, _, actions = controlled
    reply = core.request_control(bad, source="gesture")
    assert "Неподдерживаемое" in reply.text
    assert not actions


@pytest.mark.parametrize("source", ["model", "import", "voice", "typed", None, []])
def test_only_trusted_gesture_adapter_has_control_entry(controlled, source):
    core, _, actions = controlled
    assert core.owner.verify(123).success
    core.request_control("volume_up", source=source)
    assert not actions
