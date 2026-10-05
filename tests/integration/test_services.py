from __future__ import annotations

import asyncio
from collections.abc import Callable

from i3bridge.bridge.ports import ChannelEvent, LocateInfo, TellEvent
from i3bridge.config import Config
from i3bridge.i3 import packets as pk
from i3bridge.i3.client import I3Client
from i3bridge.i3.services import Dispatcher, Outbound
from i3bridge.state.db import StateStore

from .conftest import run_client, stop_client
from .fake_router import FakeRouter


class FakeFrontend:
    def __init__(self) -> None:
        self.channel: list[ChannelEvent] = []
        self.tells: list[TellEvent] = []
        self.replies: list[tuple[str, object]] = []
        self.known_users = {"alice": ("Alice", 1)}
        self.event = asyncio.Event()

    def _mark(self) -> None:
        self.event.set()

    async def on_channel(self, e: ChannelEvent) -> None:
        self.channel.append(e)
        self._mark()

    async def on_tell(self, e: TellEvent) -> bool:
        if e.to_user.lower() not in self.known_users:
            return False
        self.tells.append(e)
        self._mark()
        return True

    async def on_who_reply(self, to_user: str, from_mud: str, entries: list[pk.WhoEntry]) -> None:
        self.replies.append(("who", entries))
        self._mark()

    async def on_finger_reply(self, to_user: str, from_mud: str, r: pk.FingerReply) -> None:
        self.replies.append(("finger", r))
        self._mark()

    async def on_locate_reply(self, to_user: str, from_mud: str, r: pk.LocateReply) -> None:
        self.replies.append(("locate", r))
        self._mark()

    async def on_chan_who_reply(self, to_user: str, from_mud: str, r: pk.ChanWhoReply) -> None:
        self.replies.append(("chanwho", r))
        self._mark()

    async def on_error(self, to_user: str, from_mud: str, err: pk.ErrorPacket) -> None:
        self.replies.append(("error", err))
        self._mark()

    # presence
    def who_list(self) -> list[pk.WhoEntry]:
        return [pk.WhoEntry("Alice", 12, "[dnd] Playing Nethack")]

    def finger(self, username: str) -> pk.FingerReply | None:
        if username.lower() != "alice":
            return None
        return pk.FingerReply("Alice", "the Admin", "", "", "today", 12, "", "", "")

    def locate(self, username: str) -> LocateInfo | None:
        return LocateInfo("Alice", 12, "inactive") if username.lower() == "alice" else None

    def chan_who(self, channel: str) -> list[str] | None:
        return ["Alice"] if channel == "intergossip" else None

    def chan_user(self, username: str) -> tuple[str, int] | None:
        return self.known_users.get(username.lower())

    async def wait(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.event.wait(), timeout)
        self.event.clear()


class OtherMud:
    """A second mud on the fake router, driven directly."""

    def __init__(self, router: FakeRouter, name: str = "Foo") -> None:
        self.router = router
        self.name = name

    async def send(self, lpc: list[object]) -> None:
        await self.router.send_to("Discord", lpc)  # type: ignore[arg-type]


async def test_dispatcher_end_to_end(
    router: FakeRouter, make_config: Callable[..., Config], store: StateStore
) -> None:
    fe = FakeFrontend()
    store.add_ignore("spammer@badmud")
    disp = Dispatcher(fe, fe, ignore_patterns=store.list_ignores)
    client = I3Client(make_config(router), store, disp)
    out = Outbound(client)
    task = await run_client(client)
    await out.listen("intergossip", True)
    await router.wait_for("channel-listen")

    # inbound channel message, emote, targeted emote
    await router.send_to(
        "Discord", ["channel-m", 5, "Foo", "bob", 0, 0, "intergossip", "Bob", "hello"]
    )
    await fe.wait()
    assert fe.channel[-1] == ChannelEvent("intergossip", "Foo", "bob", "Bob", "hello", "message")
    await router.send_to(
        "Discord", ["channel-e", 5, "Foo", "bob", 0, 0, "intergossip", "Bob", "$N waves"]
    )
    await fe.wait()
    assert fe.channel[-1].kind == "emote" and fe.channel[-1].text == "$N waves"
    await router.send_to(
        "Discord",
        [
            "channel-t",
            5,
            "Foo",
            "bob",
            0,
            0,
            "intergossip",
            "Discord",
            "alice",
            "$N pokes $O",
            "$N pokes you",
            "Bob",
            "Alice",
        ],
    )
    await fe.wait()
    assert fe.channel[-1].text == "Bob@Foo pokes Alice@Discord"

    # ignored source is dropped silently
    await router.send_to(
        "Discord", ["channel-m", 5, "BadMud", "spammer", 0, 0, "intergossip", "Spammer", "buy"]
    )
    await router.send_to(
        "Discord", ["channel-m", 5, "Foo", "bob", 0, 0, "intergossip", "Bob", "after"]
    )
    await fe.wait()
    assert [e.text for e in fe.channel[-2:]] == ["Bob@Foo pokes Alice@Discord", "after"]

    # tell to known user delivered; unknown user gets unk-user back to the sender
    await router.send_to("Discord", ["tell", 5, "Foo", "bob", "Discord", "alice", "Bob", "psst"])
    await fe.wait()
    assert fe.tells[-1] == TellEvent("Foo", "bob", "Bob", "alice", "psst", False)
    await router.send_to("Discord", ["tell", 5, "Foo", "bob", "Discord", "nobody", "Bob", "psst"])
    err = await router.wait_for("error")
    assert err.target_mud == "Foo" and err.target_user == "bob" and err.payload[0] == "unk-user"

    # who-req / finger-req / locate-req / chan-who-req / chan-user-req answered from presence
    await router.send_to("Discord", ["who-req", 5, "Foo", "bob", "Discord", 0])
    who = await router.wait_for("who-reply")
    assert who.payload == [[["Alice", 12, "[dnd] Playing Nethack"]]]
    await router.send_to("Discord", ["finger-req", 5, "Foo", "bob", "Discord", 0, "alice"])
    finger = await router.wait_for("finger-reply")
    assert finger.payload[0] == "Alice" and finger.payload[3] == 0  # email withheld
    await router.send_to("Discord", ["finger-req", 5, "Foo", "bob", "Discord", 0, "zed"])
    assert (await router.wait_for("error")).payload[0] == "unk-user"
    await router.send_to("Discord", ["locate-req", 5, "Foo", "bob", 0, 0, "alice"])
    loc = await router.wait_for("locate-reply")
    assert loc.payload == ["Discord", "Alice", 12, "inactive"]
    await router.send_to("Discord", ["chan-who-req", 5, "Foo", "bob", "Discord", 0, "intergossip"])
    assert (await router.wait_for("chan-who-reply")).payload == ["intergossip", ["Alice"]]
    await router.send_to("Discord", ["chan-who-req", 5, "Foo", "bob", "Discord", 0, "nope"])
    assert (await router.wait_for("error")).payload[0] == "unk-channel"
    await router.send_to("Discord", ["chan-user-req", 5, "Foo", 0, "Discord", 0, "alice"])
    assert (await router.wait_for("chan-user-reply")).payload == ["alice", "Alice", 1]

    # replies to our own queries reach the frontend
    await router.send_to(
        "Discord", ["who-reply", 5, "Foo", 0, "Discord", "alice", [["Bob", 1, "x"]]]
    )
    await fe.wait()
    assert fe.replies[-1][0] == "who"
    await router.send_to(
        "Discord", ["error", 5, "Foo", 0, "Discord", "alice", "unk-user", "no bob", 0]
    )
    await fe.wait()
    assert fe.replies[-1][0] == "error"

    # unsupported and unknown types bounce the right error codes
    await router.send_to("Discord", ["oob-req", 5, "Foo", 0, "Discord", 0])
    assert (await router.wait_for("error")).payload[0] == "not-imp"
    await router.send_to("Discord", ["frobnicate", 5, "Foo", 0, "Discord", 0])
    assert (await router.wait_for("error")).payload[0] == "unk-type"

    # ucache updates fill in visnames for later channel messages without one
    await router.send_to("Discord", ["ucache-update", 5, "Foo", 0, 0, 0, "bob", "Bobby", 0])
    await asyncio.sleep(0.05)
    await router.send_to(
        "Discord", ["channel-m", 5, "Foo", "bob", 0, 0, "intergossip", 0, "no visname"]
    )
    await fe.wait()
    assert fe.channel[-1].visname == "Bobby"

    # outbound helpers produce spec-shaped packets
    await out.tell("alice", "Alice", "Foo", "bob", "hi bob")
    # Foo is not connected, so the router answers unk-dst to alice
    await fe.wait()
    assert fe.replies[-1][0] == "error" and fe.replies[-1][1].code == "unk-dst"  # type: ignore[union-attr]
    await stop_client(client, task)
