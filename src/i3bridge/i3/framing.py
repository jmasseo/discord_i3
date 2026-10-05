"""MudMode framing: a 4-byte big-endian length, then the LPC text, then a NUL byte.

The length counts the NUL. On input we tolerate a missing NUL (some implementations omit
it) and strip it when present. An oversize frame is unrecoverable for the stream, so
:class:`FramingError` means "drop the connection and reconnect".
"""

from __future__ import annotations

import asyncio
import struct
from typing import Final

HEADER: Final = struct.Struct(">I")
DEFAULT_MAX_PACKET: Final = 2 * 1024 * 1024


class FramingError(Exception):
    """A frame header that cannot be trusted; the stream must be closed."""


def frame(payload: bytes) -> bytes:
    """Wrap an encoded packet for the wire."""
    return HEADER.pack(len(payload) + 1) + payload + b"\0"


def _strip_nul(payload: bytes) -> bytes:
    return payload[:-1] if payload.endswith(b"\0") else payload


class FrameParser:
    """Incremental, allocation-light parser for a byte stream of MudMode frames."""

    __slots__ = ("_buf", "max_packet")

    def __init__(self, max_packet: int = DEFAULT_MAX_PACKET) -> None:
        self._buf = bytearray()
        self.max_packet = max_packet

    def feed(self, data: bytes) -> list[bytes]:
        """Append ``data`` and return every complete payload now available (NUL stripped)."""
        self._buf += data
        out: list[bytes] = []
        while len(self._buf) >= HEADER.size:
            (length,) = HEADER.unpack_from(self._buf, 0)
            if length > self.max_packet:
                raise FramingError(f"frame of {length} bytes exceeds limit {self.max_packet}")
            end = HEADER.size + length
            if len(self._buf) < end:
                break
            out.append(_strip_nul(bytes(self._buf[HEADER.size : end])))
            del self._buf[:end]
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


async def read_frame(reader: asyncio.StreamReader, max_packet: int = DEFAULT_MAX_PACKET) -> bytes:
    """Read one frame from an asyncio stream. Raises ``IncompleteReadError`` on EOF."""
    (length,) = HEADER.unpack(await reader.readexactly(HEADER.size))
    if length > max_packet:
        raise FramingError(f"frame of {length} bytes exceeds limit {max_packet}")
    return _strip_nul(await reader.readexactly(length))
