"""Render packets for logs with credentials and private message bodies removed."""

from __future__ import annotations

from .lpc import LPCValue, encode

REDACTED = "<redacted>"


def redact_lpc(value: LPCValue) -> LPCValue:
    """Return a copy of ``value`` that is safe to log.

    Router passwords and tell bodies are masked; a packet quoted inside an ``error`` is
    redacted recursively.
    """
    if not (isinstance(value, list) and len(value) >= 6 and isinstance(value[0], str)):
        return value
    v: list[LPCValue] = list(value)
    t = v[0]
    if t == "startup-req-3" and len(v) > 6 and v[6] not in (0, None):
        v[6] = REDACTED
    elif (t == "startup-reply" and len(v) > 7 and v[7] not in (0, None)) or (
        t in ("tell", "emoteto") and len(v) > 7
    ):
        v[7] = REDACTED
    elif t == "error" and len(v) > 8:
        v[8] = redact_lpc(v[8])
    return v


def redacted_text(value: LPCValue) -> str:
    return encode(redact_lpc(value))
