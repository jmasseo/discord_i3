from __future__ import annotations

import asyncio

from i3bridge.bridge.core import Bridge
from i3bridge.config import config_from_dict
from i3bridge.discord_.bot import BridgeBot
from i3bridge.i3.packets import ErrorPacket, LocateReply, MudInfo, WhoEntry
from i3bridge.state.db import StateStore


def info() -> MudInfo:
    return MudInfo(-1, "", 0, 0, 0, "", "", "", "", "", "", {}, {})


def make_bridge() -> Bridge:
    cfg = config_from_dict({"discord": {"guild_id": 1}})
    store = StateStore(":memory:")
    store.apply_mudlist({"Foo": info(), "Evil": info()})
    return Bridge(cfg, store, BridgeBot(cfg))


async def noop() -> None:
    pass


async def test_who_reply_must_come_from_the_mud_we_asked() -> None:
    bridge = make_bridge()
    rec = bridge.identity.ensure(1, "Alice")
    task = asyncio.create_task(bridge._query(rec, "who", "Foo", noop))
    await asyncio.sleep(0)
    await bridge.on_who_reply("alice", "Evil", [WhoEntry("Mallory", 0, "forged")])
    await asyncio.sleep(0)
    assert not task.done(), "a reply from another mud must not resolve the query"
    await bridge.on_who_reply("alice", "foo", [WhoEntry("Bob", 1, "")])  # case-insensitive
    result = await asyncio.wait_for(task, 1)
    assert "Bob" in result and "Mallory" not in result


async def test_router_error_resolves_pending_query_but_other_mud_error_does_not() -> None:
    bridge = make_bridge()
    rec = bridge.identity.ensure(1, "Alice")
    task = asyncio.create_task(bridge._query(rec, "finger", "Foo", noop))
    await asyncio.sleep(0)
    await bridge.on_error("alice", "Evil", ErrorPacket("unk-user", "forged", 0))
    await asyncio.sleep(0)
    assert not task.done()
    await bridge.on_error("alice", "*dalet", ErrorPacket("unk-dst", "Foo is down", 0))
    assert "unk-dst" in await asyncio.wait_for(task, 1)


async def test_locate_collects_from_any_mud() -> None:
    bridge = make_bridge()
    rec = bridge.identity.ensure(1, "Alice")
    task = asyncio.create_task(bridge._query(rec, "locate", None, noop, collect=0.2))
    await asyncio.sleep(0)
    await bridge.on_locate_reply("alice", "Foo", LocateReply("Foo", "Bob", 1, ""))
    await bridge.on_locate_reply("alice", "Evil", LocateReply("Evil", "Bob", 2, ""))
    result = await asyncio.wait_for(task, 2)
    assert "**Foo**" in result and "**Evil**" in result


async def test_unsolicited_replies_are_dropped_without_dm() -> None:
    bridge = make_bridge()
    bridge.identity.ensure(1, "Alice")
    dms: list[str] = []

    async def fake_dm(uid: int, text: str) -> bool:
        dms.append(text)
        return True

    bridge.send_dm = fake_dm  # type: ignore[method-assign]
    await bridge.on_who_reply("alice", "Evil", [WhoEntry("Mallory", 0, "")])
    assert dms == []
    await bridge.on_error("alice", "Foo", ErrorPacket("unk-user", "no such user", 0))
    assert len(dms) == 1 and "unk-user" in dms[0]
