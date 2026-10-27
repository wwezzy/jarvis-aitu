"""Opt-in current facts have no write authority or private-source discovery."""
import copy
import json

import pytest

from desktop.chat import LocalChat, bounded_plan_context


def context():
    return {"today": {"date": "2026-10-28", "items": [
        {"title": "Алгоритмы", "time": None, "status": "unknown", "provenance": "unknown"}]},
        "tomorrow": {"date": "2026-10-29", "items": []},
        "gym_days": [1, 3, 6],
        "observations": {"work_hint_minutes": 10, "gaming_minutes": 20, "observed_minutes": 35}}


def test_plan_context_retains_dates_uncertainty_and_self_report_limits():
    original = context()
    projected = bounded_plan_context(original)
    assert original == context()
    assert "2026-10-28" in projected and "Алгоритмы" in projected
    assert '"status":"unknown"' in projected and "не доказательство" in projected
    assert "Посещение зала" in projected and "только для чтения" in projected
    assert bounded_plan_context() == ""


@pytest.mark.parametrize("field,value", [
    ("gym_days", [[1]]), ("gym_days", [True]), ("gym_days", [7]),
    ("gym_days", [1, 1]), ("observations", {"private_process": "SECRET"}),
    ("observations", {"work_hint_minutes": 10, "gaming_minutes": 20, "observed_minutes": 5}),
    ("today", {"date": "2026-02-30", "items": []}),
])
def test_unvalidated_plan_metadata_is_rejected(field, value):
    supplied = context()
    supplied[field] = value
    with pytest.raises(ValueError):
        bounded_plan_context(supplied)


def test_unknown_fields_and_observation_inferred_completion_are_rejected():
    supplied = context()
    supplied["notes"] = "Never implicitly send notes"
    with pytest.raises(ValueError):
        bounded_plan_context(supplied)
    supplied = context()
    supplied["today"]["items"][0]["status"] = "done"
    with pytest.raises(ValueError):
        bounded_plan_context(supplied)
    supplied["today"]["items"][0]["provenance"] = "self_report"
    assert '"status":"done"' in bounded_plan_context(supplied)


def test_plan_is_only_explicit_system_data_and_original_question_is_preserved():
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def geturl(self):
            from desktop.chat import ENDPOINT
            return ENDPOINT
        def read(self, _size):
            return json.dumps({"done": True, "done_reason": "stop", "message": {"content": "Один следующий шаг."}}).encode()
    class Opener:
        def open(self, request, timeout):
            requests.append(json.loads(request.data))
            return Response()
    client = LocalChat(opener=Opener())
    supplied = context()
    before = copy.deepcopy(supplied)
    client.answer("Что важнее?", plan_context=supplied)
    assert supplied == before
    messages = requests[-1]["messages"]
    assert len(messages) == 2 and messages[-1] == {"role": "user", "content": "Что важнее?"}
    assert "Алгоритмы" in messages[0]["content"] and "tools" not in requests[-1]
    client.answer("Без плана")
    assert "Алгоритмы" not in requests[-1]["messages"][0]["content"]


def test_core_sends_current_plan_only_after_opt_in_and_excludes_notes(tmp_path, monkeypatch):
    from desktop.core import DesktopCore
    core = DesktopCore(tmp_path, executor=lambda _: pytest.fail("Model facts never invoke actions"))
    today = core.organizer.day()["date"]
    core.organizer.add_item("CURRENT_PLAN_ONLY", today)
    core.store.capture_note("PRIVATE_NOTE_NEVER_IMPLICIT")
    core.store.set_preferences({"local_chat_enabled": True})
    calls = []
    def answer(_client, question, **kwargs):
        calls.append((question, kwargs))
        return "Нужен один следующий шаг."
    monkeypatch.setattr("desktop.chat.LocalChat.answer", answer)
    core.handle("Как подойти к выбору?")
    assert "plan_context" not in calls[-1][1]
    core.store.set_preferences({"local_chat_plan_context": True})
    reply = core.handle("Что посоветуешь в моём случае?")
    supplied = calls[-1][1]["plan_context"]
    assert supplied["today"]["date"] == today
    assert supplied["today"]["items"][0]["title"] == "CURRENT_PLAN_ONLY"
    assert supplied["today"]["items"][0]["status"] == "unknown"
    assert "PRIVATE_NOTE" not in json.dumps(supplied)
    assert supplied["observations"] is None
    assert reply.action is None and reply.confirmation_id is None
    core.handle("Сколько весит слон?")
    assert "plan_context" not in calls[-1][1]
    core.close()


def test_followup_uses_fresh_plan_only_when_recent_dialogue_is_personal():
    from desktop.core import _needs_plan_context
    history = [{"role": "user", "content": "Как мне выбрать цель на завтра?"},
               {"role": "assistant", "content": "Выбери одно дело."}]
    assert _needs_plan_context("А почему?", history)
    assert not _needs_plan_context("Сколько весит слон?", history)
    assert not _needs_plan_context("Почему?", [{"role": "user", "content": "Что такое граф?"}])


def test_core_accounts_once_for_free_question_and_zero_for_offline_routes(tmp_path, monkeypatch):
    from desktop.core import DesktopCore
    from desktop.chat import LocalChatUnavailable
    core = DesktopCore(tmp_path, executor=lambda _: "Fixed synthetic success")
    core.store.set_preferences({"local_chat_enabled": True})
    for command in ("привет", "план на завтра", "добавь задачу прочитать", "открой калькулятор", "фокус 1 минуту"):
        core.handle(command)
    assert core.usage_snapshot()["requests"] == 0
    def answer(client, question, **kwargs):
        client.last_usage = {"input_tokens": 25, "output_tokens": 12, "elapsed_ms": 250}
        return "Короткий ответ."
    monkeypatch.setattr("desktop.chat.LocalChat.answer", answer)
    core.handle("Объясни, зачем нужны графы?")
    totals = core.usage_snapshot()
    assert totals["requests"] == 1 and totals["known_input_tokens"] == 25 and totals["known_output_tokens"] == 12
    def failed(client, *_a, **_k):
        client.last_usage = None
        raise LocalChatUnavailable("fixed")
    monkeypatch.setattr("desktop.chat.LocalChat.answer", failed)
    core.handle("Объясни подробнее")
    totals = core.usage_snapshot()
    assert totals["requests"] == 2 and totals["failed"] == 1 and totals["unknown_usage_requests"] == 1
    core.close()
