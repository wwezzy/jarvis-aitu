"""Only an explicit morning request and completed cue permit finite music."""
import threading

import pytest

from desktop.core import DesktopCore


@pytest.fixture
def local(tmp_path):
    actions = []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Тестовая музыка.")
    yield core, actions
    core.close()


@pytest.mark.parametrize("phrase", ["доброе утро", "Джарвис, доброе утро!", "good morning", "Jarvis good morning"])
def test_morning_proposes_recording_without_starting_music(local, phrase):
    core, actions = local
    reply = core.handle(phrase)
    assert reply.media_cue == "welcome_home" and len(reply.followup_id) == 32
    assert reply.confirmation_id is None and reply.action is None and not actions
    result = core.complete_morning(reply.followup_id)
    assert result.speak is False and actions == ["music_morning"]
    core.complete_morning(reply.followup_id)
    assert actions == ["music_morning"]


@pytest.mark.parametrize("transition", ["cancel", "new_request", "new_morning", "close", "expiry", "event"])
def test_canceled_stale_or_superseded_morning_does_not_start_music(local, monkeypatch, transition):
    core, actions = local
    token = core.handle("доброе утро").followup_id
    event = threading.Event()
    if transition == "cancel":
        core.cancel_morning(token)
    elif transition == "new_request":
        core.handle("как дела")
    elif transition == "new_morning":
        core.handle("доброе утро")
    elif transition == "close":
        core.close()
    elif transition == "expiry":
        core._morning_pending[token] = 0
    else:
        event.set()
    assert core.complete_morning(token, cancel_event=event).speak is False
    assert actions == []


def test_live_owner_guard_survives_until_native_music_dispatch(local, monkeypatch):
    core, actions = local
    core.owner.set_enabled(True)
    active = [True]
    monkeypatch.setattr(core.owner, "allowed", lambda: active[0])
    token = core.handle("доброе утро").followup_id
    guards = []
    def native(action, *, authorization_guard):
        assert action == "music_morning" and authorization_guard() is True
        active[0] = False  # Owner revoked during expensive player verification.
        guards.append(authorization_guard())
        return "Доступ отозван."
    core.executor, core._native_executor = native, True
    result = core.complete_morning(token)
    assert result.speak is False and guards == [False] and actions == []


@pytest.mark.parametrize("phrase", ['«доброе утро»', "не доброе утро", "доброе утро и выключи ноутбук"])
def test_morning_does_not_extract_workflows_from_quotes_negation_or_compounds(local, phrase):
    core, actions = local
    result = core.handle(phrase)
    assert result.followup_id is None and not actions


def test_canceled_request_has_no_user_history_or_side_effect(local):
    core, actions = local
    event = threading.Event()
    event.set()
    reply = core.handle("открой калькулятор", cancel_event=event)
    assert not reply.speak and core.snapshot()["recent"] == [] and actions == []
