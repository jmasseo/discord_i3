"""LPC (MudOS ``save_variable``) serialisation used by InterMUD-3 "MudMode" packets.

Grammar as produced by MudOS/FluffOS ``save_variable`` and read by ``restore_variable``::

    value   := int | float | string | array | mapping
    int     := -?[0-9]+
    float   := -?[0-9]+ "." [0-9]+ ([eE] [-+]? [0-9]+)?
    string  := '"' chars '"'            backslash escapes the next character, whatever it is
    array   := "({" (value ",")* "})"
    mapping := "([" (value ":" value ",")* "])"

The integer ``0`` doubles as the null value wherever a string, array or mapping is expected;
it decodes to ``0`` and Python ``None`` encodes to ``0``. The encoder emits no whitespace and
always writes the trailing comma MudOS writes; the decoder tolerates whitespace between
tokens. Escaping follows MudOS ``restore_string``: ``\\x`` means the literal character ``x``
for any ``x`` (so ``\\n`` is the letter n, not a newline); raw control characters pass
through unchanged. See ``docs/PROTOCOL-NOTES.md``.
"""

from __future__ import annotations

import math
from typing import Final

type LPCValue = int | float | str | list[LPCValue] | dict[LPCKey, LPCValue] | None
type LPCKey = int | float | str

MAX_DEPTH: Final = 64
MAX_NUMBER_CHARS: Final = 40  # longest numeric literal we accept (int64 is 20 digits)


class LPCError(ValueError):
    """Raised for values that cannot be encoded or text that cannot be decoded."""


# --------------------------------------------------------------------------- encoding


def encode(value: LPCValue) -> str:
    """Serialise ``value`` to LPC save format."""
    parts: list[str] = []
    _encode(value, parts, 0)
    return "".join(parts)


def encode_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _encode(value: LPCValue, parts: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise LPCError("nesting too deep")
    if value is None:
        parts.append("0")
    elif isinstance(value, bool):  # bool is an int subclass; keep it 0/1
        parts.append("1" if value else "0")
    elif isinstance(value, int):
        parts.append(str(value))
    elif isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise LPCError(f"cannot encode {value!r}")
        text = repr(value)
        if "." not in text and "e" not in text and "n" not in text:
            text += ".0"
        parts.append(text)
    elif isinstance(value, str):
        parts.append(encode_string(value))
    elif isinstance(value, list | tuple):
        parts.append("({")
        for item in value:
            _encode(item, parts, depth + 1)
            parts.append(",")
        parts.append("})")
    elif isinstance(value, dict):
        parts.append("([")
        for key, item in value.items():
            if not isinstance(key, int | float | str):
                raise LPCError(f"unsupported mapping key type {type(key).__name__}")
            _encode(key, parts, depth + 1)
            parts.append(":")
            _encode(item, parts, depth + 1)
            parts.append(",")
        parts.append("])")
    else:
        raise LPCError(f"unsupported type {type(value).__name__}")


# --------------------------------------------------------------------------- decoding


def decode(text: str) -> LPCValue:
    """Parse exactly one LPC value from ``text`` (surrounding whitespace allowed)."""
    parser = _Parser(text)
    parser.skip_ws()
    value = parser.value(0)
    parser.skip_ws()
    if parser.pos != len(text):
        raise LPCError(f"trailing data at offset {parser.pos}")
    return value


def bytes_to_text(data: bytes) -> str:
    """Decode wire bytes: UTF-8 when valid, else Latin-1 (never fails)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


_WS: Final = frozenset(" \t\r\n")
_DIGITS: Final = frozenset("0123456789")


class _Parser:
    __slots__ = ("pos", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> LPCError:
        return LPCError(f"{message} at offset {self.pos}")

    def skip_ws(self) -> None:
        text, pos = self.text, self.pos
        while pos < len(text) and text[pos] in _WS:
            pos += 1
        self.pos = pos

    def peek(self) -> str:
        if self.pos >= len(self.text):
            raise self.error("unexpected end of input")
        return self.text[self.pos]

    def expect(self, literal: str) -> None:
        if not self.text.startswith(literal, self.pos):
            raise self.error(f"expected {literal!r}")
        self.pos += len(literal)

    def value(self, depth: int) -> LPCValue:
        if depth > MAX_DEPTH:
            raise self.error("nesting too deep")
        ch = self.peek()
        if ch == '"':
            return self.string()
        if ch == "(":
            if self.text.startswith("({", self.pos):
                return self.array(depth)
            if self.text.startswith("([", self.pos):
                return self.mapping(depth)
            raise self.error("expected '({' or '(['")
        if ch == "-" or ch in _DIGITS:
            return self.number()
        raise self.error(f"unexpected character {ch!r}")

    def string(self) -> str:
        text = self.text
        pos = self.pos + 1  # past opening quote
        out: list[str] = []
        start = pos
        while True:
            if pos >= len(text):
                raise self.error("unterminated string")
            ch = text[pos]
            if ch == "\\":
                out.append(text[start:pos])
                pos += 1
                if pos >= len(text):
                    raise self.error("unterminated escape")
                out.append(text[pos])
                pos += 1
                start = pos
            elif ch == '"':
                out.append(text[start:pos])
                self.pos = pos + 1
                return "".join(out)
            else:
                pos += 1

    def number(self) -> int | float:
        text = self.text
        start = pos = self.pos
        if text[pos] == "-":
            pos += 1
        if pos >= len(text) or text[pos] not in _DIGITS:
            raise self.error("malformed number")
        while pos < len(text) and text[pos] in _DIGITS:
            pos += 1
        is_float = False
        if pos < len(text) and text[pos] == ".":
            is_float = True
            pos += 1
            if pos >= len(text) or text[pos] not in _DIGITS:
                raise self.error("malformed float")
            while pos < len(text) and text[pos] in _DIGITS:
                pos += 1
        if pos < len(text) and text[pos] in "eE":
            is_float = True
            pos += 1
            if pos < len(text) and text[pos] in "+-":
                pos += 1
            if pos >= len(text) or text[pos] not in _DIGITS:
                raise self.error("malformed exponent")
            while pos < len(text) and text[pos] in _DIGITS:
                pos += 1
        self.pos = pos
        literal = text[start:pos]
        if len(literal) > MAX_NUMBER_CHARS:
            raise self.error(f"numeric literal of {len(literal)} characters is too long")
        try:
            if is_float:
                value = float(literal)
                if math.isinf(value) or math.isnan(value):
                    raise self.error("float out of range")
                return value
            return int(literal)
        except ValueError as exc:
            raise self.error(f"malformed number {literal[:20]!r}") from exc

    def array(self, depth: int) -> list[LPCValue]:
        self.expect("({")
        items: list[LPCValue] = []
        while True:
            self.skip_ws()
            if self.text.startswith("})", self.pos):
                self.pos += 2
                return items
            items.append(self.value(depth + 1))
            self.skip_ws()
            if self.text.startswith("})", self.pos):  # tolerate a missing final comma
                self.pos += 2
                return items
            self.expect(",")

    def mapping(self, depth: int) -> dict[LPCKey, LPCValue]:
        self.expect("([")
        result: dict[LPCKey, LPCValue] = {}
        while True:
            self.skip_ws()
            if self.text.startswith("])", self.pos):
                self.pos += 2
                return result
            key = self.value(depth + 1)
            if not isinstance(key, int | float | str):
                raise self.error("unsupported mapping key")
            self.skip_ws()
            self.expect(":")
            self.skip_ws()
            result[key] = self.value(depth + 1)
            self.skip_ws()
            if self.text.startswith("])", self.pos):
                self.pos += 2
                return result
            self.expect(",")
