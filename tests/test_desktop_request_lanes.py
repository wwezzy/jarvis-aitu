"""Known mutations never compete with conversation on the model worker."""
import pytest

from desktop.core import DesktopCore, HELP, HELP_SPOKEN


@pytest.fixture
def core(tmp_path):
    instance = DesktopCore(tmp_path, executor=lambda _action: pytest.fail("Unexpected OS action"))
    yield instance
    instance.close()


@pytest.mark.parametrize("command", [
    "capture Keep original case", "task Read a book", "archive task 1", "заверши задачу 1",
    "note My note", "remember A fact", "цель на завтра", "цель #2 выполнена", "поставь таймер 2 минуты",
    "таймер 5 минут", "добавь цель на завтра задача", "я учился 30 минут", "пропусти зал завтра",
    "включи утреннюю музыку", "без звука", "громче", "разверни хром", "закрой окно",
    "открой проект Jarvis", "запиши в обсидиан Мою мысль", "заметки обсидиан", "найди заметку идея",
    "отмена deadbeef", "подтверждаю deadbeef", 'скажи «открой хром»', "не открывай хром",
])
def test_command_admission_is_pure_and_serialized(core, command):
    assert core.request_kind(command) == "command"
    assert core.snapshot()["recent"] == []


@pytest.mark.parametrize("source", [[], {}, 3, None, "model", "clipboard"])
def test_untrusted_or_malformed_source_cannot_enter_either_model_or_action(core, source):
    assert core.request_kind("открой калькулятор", source) == "command"
    core.handle("открой калькулятор", source=source)
    assert core.snapshot()["recent"] == []


@pytest.mark.parametrize("source", ["typed", "voice"])
def test_capabilities_have_a_short_spoken_answer_and_full_readable_help(core, source):
    reply = core.handle("что ты умеешь", source=source)
    assert reply.text == HELP and reply.speech_text == HELP_SPOKEN
    assert len(reply.speech_text) < len(reply.text) // 2
    assert core.snapshot()["recent"][-1]["text"] == HELP


@pytest.mark.parametrize("source", ["typed", "voice"])
def test_hearing_check_acknowledges_actual_input_kind_without_a_model(core, source):
    assert core.request_kind("ты меня слышишь", source) == "command"
    reply = core.handle("ты меня слышишь", source=source)
    assert ("голосовой запрос распознан" in reply.text) == (source == "voice")
    assert "Сообщение получил" in reply.text if source == "typed" else reply.speak
