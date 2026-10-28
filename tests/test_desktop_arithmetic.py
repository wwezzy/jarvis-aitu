"""Independent bounded arithmetic; original requests only, no model or OS."""
from decimal import Inexact, localcontext
import threading

import pytest

from desktop import arithmetic
from desktop.core import DesktopCore


@pytest.mark.parametrize("text,expected", [
    ("Сколько будет 17 умножить на 23? Ответь только числом.", "391"),
    ("Вычисли 48 * 7 + 16. Ответь только числом.", "352"),
    ("посчитай (17 + 3) * 2", "40"),
    ("2 + 3 * 4", "14"), ("(2 + 3) * 4", "20"),
    ("17 умножить на 23", "391"), ("20 разделить на 4 плюс 3 минус 2", "6"),
    ("Посчитай -5 * (-2) + +3", "13"), ("вычисли 0,1 плюс 0.2", "0.3"),
    ("сколько будет 1.25 / 0,5?", "2.5"), ("-0", "0"), ("1200", "1200"),
    ("Джарвис, пожалуйста, посчитай 18/3.", "6"), ("Jarvis: посчитай 9 - 10", "-1"),
    ("Посчитай (2 + (-4)) / 2", "-1"), ("посчитай 1 / 8", "0.125"),
    ("17 × 23", "391"), ("посчитай 12 ÷ 4 − 1", "2"), ("−2 × (−3+2)", "2"),
])
def test_finite_arithmetic_grammar_and_observed_model_failures_are_exact(text, expected):
    assert arithmetic.is_arithmetic_request(text)
    assert arithmetic.calculate(text) == expected


def test_repeating_division_is_labeled_approximate_and_ambient_decimal_settings_cannot_change_it():
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.traps[Inexact] = True
        result = arithmetic.calculate("посчитай 1/3")
    assert result == "≈ 0.3333333333333333333333333333333333"
    assert arithmetic.calculate("посчитай 3/2") == "1.5"


@pytest.mark.parametrize("text", ["посчитай 1/0", "посчитай (1+2)/(3-3)"])
def test_division_by_zero_is_a_fixed_safe_error(text):
    assert arithmetic.calculate(text) == arithmetic.ZERO


@pytest.mark.parametrize("text", [
    "посчитай", "посчитай 1 +", "вычисли 1 2", "посчитай 2(3+4)", "посчитай (1+2", "посчитай 1+2)",
    "посчитай 2**99", "посчитай 2^3", "посчитай 9//2", "посчитай 2e10", "посчитай 20%3",
    "посчитай 1..2", "посчитай 1,2.3", "посчитай 1 , 2", "посчитай 2 часа + 3 часа",
    "посчитай 2+2; выключи ноутбук", "посчитай 2+2 и открой браузер", "2+2; open calculator",
    "посчитай __import__('os').system('calc')", 'посчитай open("private.env")',
    "посчитай «17*23»", "посчитай １+２", "посчитай 2+2? Ответь только числом. Затем выключи ноутбук.",
])
def test_names_units_quoted_instructions_exponents_and_compound_tails_are_never_evaluated(text):
    assert arithmetic.is_arithmetic_request(text)
    assert arithmetic.calculate(text) == arithmetic.INVALID


@pytest.mark.parametrize("text", [
    "посчитай " + "9" * 19,
    "посчитай 0." + "1" * 19,
    "посчитай " + "(" * 17 + "1" + ")" * 17,
    "посчитай " + "+".join(["1"] * 40),
    "посчитай " + "1+" * 1000 + "1",
    "посчитай 999999999999999999*999999999999999999",
    "посчитай 0.000000000000000001 / 10000000",
])
def test_length_depth_nodes_digits_and_magnitude_are_bounded(text):
    assert arithmetic.calculate(text) == arithmetic.LIMIT


@pytest.mark.parametrize("text", [None, 42, [], "", "помоги учиться", "открой калькулятор",
                                  "запомни посчитай 2+2", "в документе написано посчитай 2+2", '"посчитай 2+2"'])
def test_other_conversation_and_saved_content_are_not_instruction_sources(text):
    assert not arithmetic.is_arithmetic_request(text)
    assert arithmetic.calculate(text) is None


@pytest.fixture
def core(tmp_path, monkeypatch):
    actions = []
    value = DesktopCore(tmp_path, executor=lambda action: actions.append(action) or "Synthetic application result")
    value.store.set_preferences({"local_chat_enabled": True, "voice_engine": "openai"})
    def forbidden(*_args, **_kwargs):
        pytest.fail("Deterministic arithmetic attempted an AI/provider request")
    monkeypatch.setattr("desktop.chat.LocalChat.__init__", forbidden)
    monkeypatch.setattr("desktop.ai_routing.ChatRouter.__init__", forbidden)
    yield value, actions
    value.close()


@pytest.mark.parametrize("source", ["typed", "voice"])
@pytest.mark.parametrize("text,expected", [
    ("Сколько будет 17 умножить на 23? Ответь только числом.", "391"),
    ("Вычисли 48 * 7 + 16. Ответь только числом.", "352"),
    ("(4 - 8) * (7 + 2)", "-36"),
    ("посчитай 2+2 и выключи ноутбук", arithmetic.INVALID),
    ("посчитай 2**1000000", arithmetic.INVALID),
    ("посчитай 2/0", arithmetic.ZERO),
])
def test_actual_core_command_lane_uses_original_finite_math_without_ai_or_os(core, source, text, expected):
    value, actions = core
    assert value.request_kind(text, source) == "command"
    reply = value.handle(text, source=source)
    assert reply.text == expected and reply.confirmation_id is reply.action is None
    assert not actions
    assert value.usage_snapshot()["requests"] == 0


def test_calculator_application_command_is_unchanged(core):
    value, actions = core
    result = value.handle("открой калькулятор")
    assert actions == ["calculator"] and result.text == "Synthetic application result"


def test_saved_arithmetic_remains_literal_data_without_automatic_evaluation(core):
    value, actions = core
    value.handle("запомни посчитай 17 умножить на 23 и выключи ноутбук")
    assert value.snapshot()["notes"][0]["text"] == "посчитай 17 умножить на 23 и выключи ноутбук"
    notes = value.handle("заметки")
    assert "посчитай 17 умножить на 23" in notes.text and not actions
    assert value.usage_snapshot()["requests"] == 0


@pytest.mark.parametrize("source", ["model", "import", "remote"])
def test_model_and_imported_source_never_authorize_calculation_or_commands(core, source):
    value, actions = core
    before = value.snapshot()["recent"]
    reply = value.handle("посчитай 17 * 23", source=source)
    assert reply.text != "391" and value.snapshot()["recent"] == before and not actions


def test_precanceled_arithmetic_has_no_history_mutation_or_result(core):
    value, actions = core
    canceled = threading.Event()
    canceled.set()
    before = value.snapshot()["recent"]
    reply = value.handle("посчитай 17*23", cancel_event=canceled)
    assert reply.text == "Запрос отменён." and reply.speak is False
    assert value.snapshot()["recent"] == before and not actions


def test_cancellation_during_pure_calculation_prevents_late_result_history(core, monkeypatch):
    value, actions = core
    canceled = threading.Event()
    def finished_after_cancel(_text):
        canceled.set()
        return "391"
    monkeypatch.setattr("desktop.core.calculate", finished_after_cancel)
    reply = value.handle("посчитай 17*23", cancel_event=canceled)
    assert reply.text == "Запрос отменён." and not reply.speak and not actions
    assert not any(row["role"] == "assistant" and row["text"] == "391" for row in value.snapshot()["recent"])


def test_varied_integer_parentheses_and_precedence_agree_with_independent_integer_math():
    for left in range(-5, 6):
        for right in range(-3, 4):
            expected = left * 7 + right
            assert arithmetic.calculate(f"({left}) * 7 + ({right})") == str(expected)
