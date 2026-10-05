from __future__ import annotations

import contextlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from i3bridge.i3 import lpc
from i3bridge.i3.lpc import LPCError, bytes_to_text, decode, encode

STARTUP = [
    "startup-req-3", 5, "Discord", 0, "*dalet", 0,
    0, 0, 0, 0, 0, 0,
    "i3bridge 0.1", "i3bridge", "Python/asyncio", "Discord", "open for public",
    "admin@example.com", {"tell": 1, "channel": 1}, 0,
]  # fmt: skip


def test_encode_golden_startup() -> None:
    assert encode(STARTUP) == (
        '({"startup-req-3",5,"Discord",0,"*dalet",0,0,0,0,0,0,0,"i3bridge 0.1","i3bridge",'
        '"Python/asyncio","Discord","open for public","admin@example.com",'
        '(["tell":1,"channel":1,]),0,})'
    )


def test_round_trip_startup() -> None:
    assert decode(encode(STARTUP)) == STARTUP


def test_none_encodes_as_zero_and_bool_as_int() -> None:
    assert encode([None, True, False]) == "({0,1,0,})"


def test_string_escapes() -> None:
    assert encode('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'
    assert decode('"say \\"hi\\" \\\\ bye"') == 'say "hi" \\ bye'


def test_backslash_x_means_x_like_mudos() -> None:
    # MudOS restore_string: "\n" in the text is the letter n, a raw newline is a newline.
    assert decode('"a\\nb"') == "anb"
    assert decode('"a\nb"') == "a\nb"
    assert encode("a\nb") == '"a\nb"'


def test_whitespace_and_missing_trailing_comma_tolerated() -> None:
    text = (
        ' ({ "mudlist" , 5 , "*i4", 0, "Discord", 0, 42, '
        '([ "Foo" : ({ -1, "1.2.3.4", 4000 }) ]) }) '
    )
    value = decode(text)
    assert value == ["mudlist", 5, "*i4", 0, "Discord", 0, 42, {"Foo": [-1, "1.2.3.4", 4000]}]


def test_empty_containers() -> None:
    assert encode([]) == "({})"
    assert encode({}) == "([])"
    assert decode("({})") == []
    assert decode("([])") == {}
    assert decode("({ })") == []


def test_numbers() -> None:
    assert decode("-12") == -12
    assert decode("3.5") == 3.5
    assert decode("1e+20") == 1e20
    assert encode(2.0) == "2.0"
    assert encode(1e20) == "1e+20"
    assert decode(encode(1e20)) == 1e20


@pytest.mark.parametrize(
    "bad",
    [
        '"unterminated',
        '"bad escape \\',
        "({1,2",
        "([1:2",
        "([ ({1,}) : 2, ])",
        "({1 2})",
        "1 2",
        "-",
        "1.",
        "1e",
        "(x",
        "",
        "abc",
    ],
)
def test_decode_errors(bad: str) -> None:
    with pytest.raises(LPCError):
        decode(bad)


def test_depth_limit() -> None:
    bomb = "({" * (lpc.MAX_DEPTH + 2) + "})" * (lpc.MAX_DEPTH + 2)
    with pytest.raises(LPCError):
        decode(bomb)
    nested: list[object] = []
    cur = nested
    for _ in range(lpc.MAX_DEPTH + 2):
        inner: list[object] = []
        cur.append(inner)
        cur = inner
    with pytest.raises(LPCError):
        encode(nested)  # type: ignore[arg-type]


def test_encode_rejects_nan_and_unknown_types() -> None:
    with pytest.raises(LPCError):
        encode(float("nan"))
    with pytest.raises(LPCError):
        encode({(1, 2): 3})  # type: ignore[dict-item]
    with pytest.raises(LPCError):
        encode(object())  # type: ignore[arg-type]


def test_bytes_to_text_prefers_utf8_then_latin1() -> None:
    assert bytes_to_text("héllo".encode()) == "héllo"
    assert bytes_to_text(b"h\xe9llo") == "héllo"


scalars = st.one_of(
    st.integers(min_value=-(2**63), max_value=2**63 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(),
)
values = st.recursive(
    scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=6),
        st.dictionaries(scalars, children, max_size=6),
    ),
    max_leaves=40,
)


@settings(max_examples=300, deadline=None)
@given(values)
def test_round_trip_property(value: object) -> None:
    assert decode(encode(value)) == value  # type: ignore[arg-type]


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_decoder_never_crashes_with_other_exceptions(text: str) -> None:
    with contextlib.suppress(LPCError):
        decode(text)
