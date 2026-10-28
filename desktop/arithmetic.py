"""Finite original-user arithmetic, independent of models and OS execution."""
from __future__ import annotations

from decimal import Context, Decimal, DecimalException, Inexact, localcontext
import re


MAX_INPUT = 256
MAX_TOKENS = 96
MAX_NODES = 64
MAX_DEPTH = 16
MAX_DIGITS = 64
_MAXIMUM = Decimal("1e24")
_MINIMUM = Decimal("1e-24")
_LEADING = re.compile(r"^(?:сколько будет|посчитай|вычисли)\b\s*:?\s*", re.I)
_FORMAT = re.compile(r"[?.!]*\s*ответь только числом[?.!]*\s*$", re.I)
_TOKENS = re.compile(r"[0-9]+(?:[.,][0-9]+)?|[+*/()-]")
_OPERATORS = (("умножить на", "*"), ("разделить на", "/"), ("плюс", "+"), ("минус", "-"))
INVALID = "Нужен один пример: числа, + − * / и скобки. Например: «посчитай (17 + 3) * 2». Другие команды задавай отдельно."
LIMIT = "Пример превышает безопасный предел: до 256 символов, 64 цифр и 16 уровней скобок; модуль результата — от 10⁻²⁴ до 10²⁴."
ZERO = "На ноль делить нельзя."


class _Limit(ValueError):
    pass


def _frame(text):
    value = text.casefold().replace("ё", "е").strip()
    value = re.sub(r"^(?:джарвис|джарсис|джарвиз|jarvis)[\s,:]+", "", value, count=1)
    return re.sub(r"^пожалуйста[\s,]+", "", value, count=1)


def _operators(value):
    value = value.translate(str.maketrans({"−": "-", "×": "*", "÷": "/"}))
    for phrase, operator in _OPERATORS:
        value = re.sub(r"\b" + phrase + r"\b", operator, value)
    return value


def is_arithmetic_request(text) -> bool:
    """Pure admission only; reported/quoted/note instructions aren't extracted."""
    if not isinstance(text, str) or not text.strip():
        return False
    value = _frame(text[:MAX_INPUT + 1])
    if _LEADING.match(value):
        return True
    # Bare numeric expressions are admitted as a whole. A numeric expression
    # with an injected tail is still rejected deterministically, never sent
    # onward as either a model request or a partially extracted PC command.
    value = _operators(value)
    return bool(any("0" <= char <= "9" for char in value) and re.fullmatch(r"[0-9+*/(),.\-\s?!]+", value, flags=re.ASCII)
                or re.match(r"^[+\-(\s]*[0-9][0-9.,\s]*[+*/()-]", value, flags=re.ASCII))


class _Parser:
    def __init__(self, text):
        self.tokens, self.index, self.nodes = [], 0, 0
        position = 0
        for match in _TOKENS.finditer(text):
            if text[position:match.start()].strip(" \t\r\n"):
                raise ValueError
            self.tokens.append(match[0])
            position = match.end()
        if text[position:].strip(" \t\r\n") or not self.tokens:
            raise ValueError
        if len(self.tokens) > MAX_TOKENS or sum(char.isdigit() for char in text) > MAX_DIGITS:
            raise _Limit

    def _node(self):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _Limit

    @staticmethod
    def _bounded(value):
        absolute = value.copy_abs()
        if not value.is_finite() or absolute > _MAXIMUM or absolute != 0 and absolute < _MINIMUM:
            raise _Limit
        return value

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def expression(self, depth=0):
        value = self.term(depth)
        while self._peek() in {"+", "-"}:
            operator = self._peek()
            self.index += 1
            right = self.term(depth)
            self._node()
            value = self._bounded(value + right if operator == "+" else value - right)
        return value

    def term(self, depth):
        value = self.primary(depth)
        while self._peek() in {"*", "/"}:
            operator = self._peek()
            self.index += 1
            right = self.primary(depth)
            self._node()
            if operator == "/" and right == 0:
                raise ZeroDivisionError
            value = self._bounded(value * right if operator == "*" else value / right)
        return value

    def primary(self, depth):
        if depth > MAX_DEPTH:
            raise _Limit
        token = self._peek()
        if token in {"+", "-"}:
            self.index += 1
            self._node()
            value = self.primary(depth + 1)
            return value.copy_negate() if token == "-" else value
        if token == "(":
            self.index += 1
            value = self.expression(depth + 1)
            if self._peek() != ")":
                raise ValueError
            self.index += 1
            return value
        if token is None or token in {"*", "/", ")"}:
            raise ValueError
        self.index += 1
        self._node()
        number = token.replace(",", ".")
        integer, _, fraction = number.partition(".")
        if len(integer) > 18 or len(fraction) > 18 or len(integer) + len(fraction) > 32:
            raise _Limit
        return self._bounded(Decimal(number))


def calculate(text) -> str | None:
    """Return bounded plain result/error only for an original arithmetic request."""
    if not is_arithmetic_request(text):
        return None
    if len(text) > MAX_INPUT:
        return LIMIT
    value = _LEADING.sub("", _frame(text), count=1)
    value = _FORMAT.sub("", value).rstrip("?.! \t\r\n")
    value = _operators(value)
    try:
        parser = _Parser(value)
        with localcontext(Context(prec=34, Emax=30, Emin=-30)) as context:
            context.clear_flags()
            result = parser.expression()
            if parser.index != len(parser.tokens):
                raise ValueError
            approximate = context.flags[Inexact]
        rendered = "0" if result == 0 else format(result, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return ("≈ " if approximate else "") + rendered
    except ZeroDivisionError:
        return ZERO
    except (_Limit, DecimalException):
        return LIMIT
    except ValueError:
        return INVALID
