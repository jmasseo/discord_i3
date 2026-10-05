from __future__ import annotations

import asyncio
import struct

import pytest

from i3bridge.i3.framing import FrameParser, FramingError, frame, read_frame


def test_frame_includes_nul_in_length() -> None:
    out = frame(b"abc")
    assert out == b"\x00\x00\x00\x04abc\x00"


def test_parser_reassembles_byte_by_byte() -> None:
    parser = FrameParser()
    data = frame(b"one") + frame(b"two")
    got: list[bytes] = []
    for i in range(len(data)):
        got += parser.feed(data[i : i + 1])
    assert got == [b"one", b"two"]
    assert parser.pending == 0


def test_parser_handles_multiple_frames_per_chunk_and_empty() -> None:
    parser = FrameParser()
    assert parser.feed(frame(b"") + frame(b"x") + b"\x00\x00") == [b"", b"x"]
    assert parser.pending == 2


def test_parser_tolerates_missing_nul() -> None:
    raw = struct.pack(">I", 3) + b"abc"
    assert FrameParser().feed(raw) == [b"abc"]


def test_parser_oversize_raises() -> None:
    parser = FrameParser(max_packet=10)
    with pytest.raises(FramingError):
        parser.feed(struct.pack(">I", 11))


async def test_read_frame_stream() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(frame(b"hello"))
    assert await read_frame(reader) == b"hello"
    reader.feed_data(struct.pack(">I", 99))
    with pytest.raises(FramingError):
        await read_frame(reader, max_packet=10)
    reader2 = asyncio.StreamReader()
    reader2.feed_data(b"\x00\x00")
    reader2.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(reader2)
