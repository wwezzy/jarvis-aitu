"""User request grammar only; no model/provider, process launch or live audio."""
import pytest

from desktop.core import DesktopCore, HELP


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    actions = []
    core = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Synthetic handler result")
    core.store.set_preferences({"local_chat_enabled": True})
    monkeypatch.setattr("desktop.chat.LocalChat.answer", lambda *_a, **_k: pytest.fail("A deterministic request fell through to the model"))
    yield core, actions
    core.close()


@pytest.mark.parametrize("text,action", [
    ("Включи Яндекс.Музыку.", "music_open"),
    ("можешь открыть яндекс.музыку", "music_open"),
    ("Джарвис: открой, пожалуйста, Яндекс. Музыку!", "music_open"),
    ("Could you please open Yandex.Music?", "music_open"),
    ("play Yandex Music", "music_open"),
    ("открой пожалуйста браузер", "browser"),
    ("Пожалуйста, Джарвис, открой мой браузер.", "browser"),
    ("можешь открыть браузер?", "browser"),
    ("открой браузер, Джарвис", "browser"),
    ("Jarvis, could you please launch my browser?", "browser"),
    ("открой гугл хром", "chrome"),
    ("можешь открыть Google Chrome?", "chrome"),
    ("please open Chrome", "chrome"),
    ("открой калькулятор пожалуйста", "calculator"),
    ("Could you open the calculator?", "calculator"),
    ("можешь открыть блокнот", "notepad"),
    ("Джарвис, запусти, пожалуйста, проводник", "explorer"),
    ("please open file explorer", "explorer"),
    ("можешь открыть ютуб", "youtube"),
    ("would you please minimize all windows", "minimize_all"),
    ("можешь развернуть окно", "maximize"),
    ("пожалуйста разверни YouTube", "maximize_youtube"),
    ("Could you turn the volume down?", "volume_down"),
    ("сделай пожалуйста погромче", "volume_up"),
    ("please mute the sound", "volume_mute"),
    ("можешь включить звук", "volume_unmute"),
    ("включи пожалуйста музыку", "music_play"),
    ("можешь включить утреннюю музыку", "music_morning"),
    ("можешь отменить выключение", "cancel_shutdown"),
])
def test_whole_positive_user_requests_route_to_finite_actions_before_ai(assistant, text, action):
    core, actions = assistant
    reply = core.handle(text, source="voice")
    assert actions == [action] and reply.action == action
    assert reply.text == "Synthetic handler result" and reply.confirmation_id is None
    assert core.usage_snapshot()["requests"] == 0


@pytest.mark.parametrize("text", [
    "что ты можешь", "расскажи что ты умеешь", "Расскажи, пожалуйста, что ты можешь?",
    "пожалуйста расскажи мне о своих возможностях", "Jarvis, what can you do?",
    "please tell me what you can do", "что ты умеешь, Джарвис", "можешь рассказать что ты умеешь",
    "можешь ли ты открыть браузер?", "ты можешь выключить ноутбук?", "are you able to open Chrome?",
    "что ты умеешь дар с",
])
def test_capability_questions_explain_capabilities_without_action_or_confirmation(assistant, text):
    core, actions = assistant
    reply = core.handle(text)
    assert reply.text == HELP and not actions
    assert reply.action is None and reply.confirmation_id is None
    assert core.usage_snapshot()["requests"] == 0


def test_opening_versus_playing_question_does_not_open_or_resume_anything(assistant):
    core, actions = assistant
    reply = core.handle("Чем отличается открыть музыку от включить музыку?")
    assert "подтверждения" in reply.text and not actions
    assert reply.action is None and reply.confirmation_id is None


@pytest.mark.parametrize("text", [
    '"открой браузер"', "«включи Яндекс.Музыку»", "открой 'браузер'", "не открой браузер",
    "не открывай Google Chrome", "не мог бы ты открыть браузер", "please do not open browser",
    "don't open calculator", "скажи открой браузер", "он сказал открой браузер",
    "в документе написано открой браузер", "the document says open browser",
    "открой браузер и выключи ноут", "открой браузер, калькулятор", "открой браузер. Перезагрузи компьютер",
    "open browser; restart computer", "можешь открыть браузер или Chrome", "open https://example.invalid",
    "открой powershell", "run rm -rf /", "перезагрузи сервер", "выключи свет", "увеличь громкость на 50%",
    "/pclock выключи", "что ты умеешь дар с",
])
def test_unsafe_ambiguous_or_unsupported_command_text_gets_clarification_not_ai(assistant, text):
    core, actions = assistant
    reply = core.handle(text)
    assert not actions and reply.action is None and reply.confirmation_id is None
    assert "Локальная модель" not in reply.text
    assert core.usage_snapshot()["requests"] == 0


@pytest.mark.parametrize("text,action", [
    ("можешь выключить ноутбук", "shutdown"),
    ("Could you shut down my computer?", "shutdown"),
    ("please restart my laptop", "restart"),
    ("можешь заблокировать компьютер", "lock"),
])
def test_explicit_power_requests_still_require_separate_one_use_confirmation(assistant, text, action):
    core, actions = assistant
    reply = core.handle(text, source="voice")
    assert not actions and reply.action == action and reply.confirmation_id
    core.confirm(reply.confirmation_id, source="voice")
    assert not actions
    core.confirm(reply.confirmation_id, source="button")
    assert actions == [action]
    core.confirm(reply.confirmation_id, source="button")
    assert actions == [action]


def test_capture_content_keeps_literal_commands_without_authorizing_them(assistant):
    core, actions = assistant
    content = "Пожалуйста, открой Яндекс.Музыку и выключи ноутбук"
    core.handle("запомни " + content)
    core.handle("добавь задачу " + content)
    assert not actions
    assert core.snapshot()["notes"][0]["text"] == content
    assert core.snapshot()["tasks"][0]["title"] == content


@pytest.mark.parametrize("source", ["model", "document", "web", "telegram"])
def test_new_natural_request_forms_do_not_expand_untrusted_source_authority(assistant, source):
    core, actions = assistant
    core.handle("можешь открыть яндекс.музыку", source=source)
    core.handle("открой пожалуйста браузер", source=source)
    assert not actions and core.snapshot()["recent"] == []


def test_non_command_conversation_still_uses_only_optional_text_model(assistant, monkeypatch):
    core, actions = assistant
    received = []
    monkeypatch.setattr("desktop.chat.LocalChat.answer", lambda _client, text, **_kwargs: received.append(text) or "Ответ про обучение.")
    question = "Как лучше понять рекурсию?"
    assert core.handle(question).text == "Локальная модель: Ответ про обучение."
    assert received == [question] and not actions


def test_old_operational_model_denials_are_excluded_without_rewriting_history(assistant, monkeypatch):
    core, actions = assistant
    core.store.set_preferences({"local_chat_history": True})
    good_question, good_answer = "Как учиться регулярно?", "Начни с одного короткого шага."
    for question, answer in ((good_question, good_answer),
                             ("открой гугл хром", "Я не могу открывать приложения."),
                             ("что ты умеешь дар с", "Мне недоступно управление компьютером.")):
        core.store.record("user", question)
        core.store.record("assistant", "Локальная модель: " + answer)
    original_records = core.snapshot()["recent"]
    received = []

    def answer(_client, _text, **kwargs):
        received.append(kwargs["history"])
        return "Продолжим учиться."

    monkeypatch.setattr("desktop.chat.LocalChat.answer", answer)
    core.handle("Как сделать привычку постоянной?")
    assert received == [[{"role": "user", "content": good_question}, {"role": "assistant", "content": good_answer}]]
    assert core.snapshot()["recent"][:len(original_records)] == original_records
    assert not actions
