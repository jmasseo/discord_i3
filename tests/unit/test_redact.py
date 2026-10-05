from __future__ import annotations

from i3bridge.i3.redact import REDACTED, redact_lpc, redacted_text


def test_redacts_credentials_and_tell_bodies_only() -> None:
    startup = [
        "startup-req-3",
        5,
        "Discord",
        0,
        "*dalet",
        0,
        123456,
        0,
        0,
        0,
        0,
        0,
        "lib",
        "b",
        "d",
        "t",
        "o",
        "e",
        {},
        0,
    ]
    out = redact_lpc(startup)
    assert isinstance(out, list) and out[6] == REDACTED and out[7:] == startup[7:]
    assert startup[6] == 123456, "input is not mutated"
    fresh = ["startup-req-3", 5, "Discord", 0, "*dalet", 0, 0, 0, 0]
    assert redact_lpc(fresh) == fresh, "a zero password is not a secret"
    reply = ["startup-reply", 5, "*dalet", 0, "Discord", 0, [["*dalet", "1.2.3.4 8787"]], 987654]
    assert redact_lpc(reply)[7] == REDACTED  # type: ignore[index]
    tell = ["tell", 5, "Foo", "bob", "Discord", "alice", "Bob", "secret words"]
    assert redact_lpc(tell)[7] == REDACTED and redact_lpc(tell)[6] == "Bob"  # type: ignore[index]
    emote = ["emoteto", 5, "Foo", "bob", "Discord", "alice", "Bob", "hugs"]
    assert redact_lpc(emote)[7] == REDACTED  # type: ignore[index]
    err = ["error", 5, "*dalet", 0, "Discord", 0, "not-allowed", "bad password", startup]
    nested = redact_lpc(err)[8]  # type: ignore[index]
    assert isinstance(nested, list) and nested[6] == REDACTED
    chan = ["channel-m", 5, "Foo", "bob", 0, 0, "ig", "Bob", "public"]
    assert redact_lpc(chan) == chan
    assert redact_lpc("not a packet") == "not a packet"
    assert REDACTED in redacted_text(tell) and "secret words" not in redacted_text(tell)
