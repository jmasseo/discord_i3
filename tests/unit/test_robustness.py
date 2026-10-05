from __future__ import annotations

import asyncio
import contextlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from i3bridge.i3 import packets as pk
from i3bridge.i3.lpc import LPCError, decode
from i3bridge.i3.services.dispatcher import UCACHE_MAX, Dispatcher
from tests.integration.test_services import FakeFrontend


@pytest.mark.parametrize(
    "literal",
    ["9" * 5000, "1" + "0" * 70, "-" + "7" * 41, "1e999999", "1." + "0" * 60 + "e5"],
)
def test_oversized_or_unrepresentable_numbers_are_lpc_errors(literal: str) -> None:
    with pytest.raises(LPCError):
        decode(literal)
    with pytest.raises(LPCError):
        decode("({" + literal + ",})")


def test_int64_sized_numbers_still_parse() -> None:
    assert decode("-9223372036854775808") == -(2**63)
    assert decode("18446744073709551616") == 2**64


class FakeClient:
    mud_name = "Discord"
    router_name = "*fake"

    def __init__(self) -> None:
        self.sent: list[pk.Packet] = []

    async def send(self, pkt: pk.Packet) -> None:
        self.sent.append(pkt)


def test_ucache_is_bounded() -> None:
    fe = FakeFrontend()
    disp = Dispatcher(fe, fe)
    client = FakeClient()

    async def run() -> None:
        for i in range(UCACHE_MAX + 500):
            pkt = pk.Packet(
                "ucache-update", f"Mud{i % 7}", None, None, None, [f"user{i}", f"User{i}", 2]
            )
            await disp.handle(client, pkt)  # type: ignore[arg-type]

    asyncio.run(run())
    assert len(disp.ucache) == UCACHE_MAX
    last = UCACHE_MAX + 499
    assert disp.visname_for(f"Mud{last % 7}", f"user{last}", "x") == f"User{last}"
    assert disp.visname_for("Mud0", "user0", "fallback") == "fallback", "oldest entries evicted"


KNOWN_TYPES = [
    "channel-m", "channel-e", "channel-t", "tell", "emoteto", "who-req", "who-reply",
    "finger-req", "finger-reply", "locate-req", "locate-reply", "chan-who-req", "chan-who-reply",
    "chan-user-req", "chan-user-reply", "error", "ucache-update", "oob-req", "mail", "bogus",
]  # fmt: skip

scalars = st.one_of(st.integers(-5, 5), st.text(max_size=8), st.just(0), st.none())
values = st.recursive(
    scalars,
    lambda c: st.one_of(
        st.lists(c, max_size=4), st.dictionaries(st.text(max_size=4), c, max_size=3)
    ),
    max_leaves=12,
)
packets = st.lists(values, min_size=6, max_size=14)


@settings(max_examples=400, deadline=None)
@given(ptype=st.sampled_from(KNOWN_TYPES), body=packets)
def test_dispatcher_only_raises_packet_errors(ptype: str, body: list[object]) -> None:
    raw: list[object] = [ptype, 5, "Foo", "bob", "Discord", "alice", *body[6:]]
    try:
        pkt = pk.Packet.from_lpc(raw)  # type: ignore[arg-type]
    except pk.PacketError:
        return
    fe = FakeFrontend()
    disp = Dispatcher(fe, fe)
    client = FakeClient()
    # the client turns PacketError into a bad-pkt reply; anything else would be a crash
    with contextlib.suppress(pk.PacketError):
        asyncio.run(disp.handle(client, pkt))  # type: ignore[arg-type]
