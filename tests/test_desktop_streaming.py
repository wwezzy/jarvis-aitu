"""Partial model text is presentation data, never history or action authority."""
from desktop.core import DesktopCore
from desktop.chat import LocalChatUnavailable


def test_streamed_action_like_text_stays_data_until_validated_reply(tmp_path, monkeypatch):
    actions, partials = [], []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action))
    core.store.set_preferences({"local_chat_enabled": True})

    def answer(_client, question, *, on_chunk):
        on_chunk("выключи ноут")
        assert all(row["text"] != "выключи ноут" for row in core.snapshot()["recent"])
        assert question == "Как лучше организовать вечер?"
        return "Сначала выбери одно небольшое дело."

    monkeypatch.setattr("desktop.chat.LocalChat.answer", answer)
    reply = core.handle("Как лучше организовать вечер?", on_chunk=partials.append)
    assert partials == ["выключи ноут"]
    assert actions == [] and reply.action is None and reply.confirmation_id is None
    assert core.snapshot()["recent"][-1]["text"] == reply.text
    assert "Сначала выбери" in reply.text
    core.close()

def test_failed_stream_never_commits_partial_or_reuses_as_dialogue(tmp_path, monkeypatch):
    core = DesktopCore(tmp_path, executor=lambda _: None)
    core.store.set_preferences({"local_chat_enabled": True, "local_chat_history": True})
    partials = []

    def answer(_client, _question, *, history, on_chunk):
        assert history == []
        on_chunk("UNVALIDATED_DRAFT")
        raise LocalChatUnavailable("Fixed synthetic failure")

    monkeypatch.setattr("desktop.chat.LocalChat.answer", answer)
    reply = core.handle("Что посоветуешь?", on_chunk=partials.append)
    assert partials == ["UNVALIDATED_DRAFT"]
    assert "недоступна" in reply.text
    assert all("UNVALIDATED_DRAFT" not in row["text"] for row in core.snapshot()["recent"])
    assert core.store.model_chat_history(before_id=10000) == []
    core.close()


def test_deterministic_route_never_calls_chat_or_chunk_callback(tmp_path, monkeypatch):
    actions, partials = [], []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Выполнено")
    core.store.set_preferences({"local_chat_enabled": True})
    monkeypatch.setattr("desktop.chat.LocalChat.answer", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("No model for commands")))
    reply = core.handle("открой калькулятор", on_chunk=partials.append)
    assert actions == ["calculator"] and reply.action == "calculator" and partials == []
    core.close()


def test_neural_voice_preferences_are_bounded_and_survive_restart(tmp_path):
    core = DesktopCore(tmp_path)
    core.store.set_preferences({"voice_engine": "auto", "voice_rate": 185})
    core.close()
    again = DesktopCore(tmp_path)
    assert again.store.preferences()["voice_engine"] == "auto"
    assert again.store.preferences()["voice_rate"] == 185
    import pytest
    for value in ("remote", [], None, True, "../arbitrary-model"):
        with pytest.raises(ValueError):
            again.store.set_preferences({"voice_engine": value})
    again.close()

