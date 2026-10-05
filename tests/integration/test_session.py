from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable

import pytest

from i3bridge.config import Config
from i3bridge.i3 import packets as pk
from i3bridge.i3.client import I3Client, NotConnectedError, State
from i3bridge.state.db import StateStore

from .conftest import RecordingHandler, run_client, stop_client
from .fake_router import FakeRouter, Registry


async def test_registers_and_syncs_lists(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    client = I3Client(make_config(router), store, handler)
    task = await run_client(client)
    req = await router.wait_for("startup-req-3")
    assert req.payload[0] == 0, "first contact must send password 0"
    assert req.payload[12] == dict(req.payload[12]) and req.payload[12]["channel"] == 1
    password = store.get_password("Discord", router.name)
    assert password == router.registry.passwords["Discord"]
    await asyncio.sleep(0.1)
    assert "intergossip" in store.get_chanlist()
    assert "Discord" in store.get_mudlist() and store.get_mudlist()["Discord"].up
    mudlist_id, chanlist_id = store.get_list_ids("Discord", router.name)
    assert (mudlist_id, chanlist_id) == (router.mudlist_id, router.chanlist_id)
    await stop_client(client, task)
    shutdown = await router.wait_for("shutdown")
    assert shutdown.payload == [0]
    assert client.state is State.DISCONNECTED


async def test_reconnect_reuses_password_and_gets_deltas(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    client = I3Client(make_config(router), store, handler)
    task = await run_client(client)
    await router.wait_for("startup-req-3")
    await asyncio.sleep(0.05)
    await stop_client(client, task)

    client2 = I3Client(make_config(router), store, handler)
    task2 = await run_client(client2)
    req = await router.wait_for("startup-req-3")
    assert req.payload[0] == router.registry.passwords["Discord"]
    assert req.payload[1] == router.mudlist_id - 1  # our stored id; router bumped on our reconnect
    await stop_client(client2, task2)


async def test_wrong_password_moves_to_next_router_and_reports(
    make_config: Callable[..., Config], store: StateStore, handler: RecordingHandler
) -> None:
    router = FakeRouter("*strict")
    router.registry.passwords["Discord"] = 424242
    await router.start()
    good = FakeRouter("*good")
    await good.start()
    try:
        errors: list[pk.ErrorPacket] = []

        async def on_err(e: pk.ErrorPacket) -> None:
            errors.append(e)

        client = I3Client(make_config(router, good), store, handler)
        client.on_router_error.append(on_err)
        task = await run_client(client)
        assert client.router_name == "*good"
        assert errors and errors[0].code == "not-allowed"
        await stop_client(client, task)
    finally:
        await router.stop()
        await good.stop()


async def test_password_from_sibling_router_is_tried(
    make_config: Callable[..., Config], store: StateStore, handler: RecordingHandler
) -> None:
    registry = Registry()
    a = FakeRouter("*a", registry=registry)
    b = FakeRouter("*b", registry=registry, accept_unknown_password=False)
    await a.start()
    await b.start()
    try:
        client = I3Client(make_config(a), store, handler)
        task = await run_client(client)
        await stop_client(client, task)
        client2 = I3Client(make_config(b), store, handler)
        task2 = await run_client(client2)
        req = await b.wait_for("startup-req-3")
        assert req.payload[0] == registry.passwords["Discord"]
        assert store.get_password("Discord", "*b") == registry.passwords["Discord"]
        await stop_client(client2, task2)
    finally:
        await a.stop()
        await b.stop()


async def test_router_redirect_is_followed(
    make_config: Callable[..., Config], store: StateStore, handler: RecordingHandler
) -> None:
    primary = FakeRouter("*primary")
    await primary.start()
    secondary = FakeRouter(
        "*secondary",
        router_list=[("*primary", f"127.0.0.1 {primary.port}"), ("*secondary", "127.0.0.1 1")],
    )
    await secondary.start()
    try:
        client = I3Client(make_config(secondary), store, handler)
        task = await run_client(client)
        assert client.router_name == "*primary"
        assert primary.connected("Discord")
        await stop_client(client, task)
    finally:
        await primary.stop()
        await secondary.stop()


async def test_reconnects_after_drop_and_relistens(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    client = I3Client(make_config(router), store, handler)
    task = await run_client(client)
    await client.listen("intergossip", True)
    listen = await router.wait_for("channel-listen")
    assert listen.payload == ["intergossip", 1]
    disconnects = 0

    async def on_disc() -> None:
        nonlocal disconnects
        disconnects += 1

    client.on_disconnect.append(on_disc)
    await router.disconnect("Discord")
    relisten = await router.wait_for("channel-listen")
    assert relisten.payload == ["intergossip", 1]
    assert disconnects == 1
    assert client.stats.reconnects >= 1
    await stop_client(client, task)


async def test_survives_garbage_and_reconnects_on_oversize(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    client = I3Client(make_config(router, i3_max_packet_bytes=4096), store, handler)
    task = await run_client(client)
    await router.send_to("Discord", ["tell", 5, "Foo", "bob", "Discord", "alice", "Bob", "hi"])
    await router.send_raw("Discord", struct.pack(">I", 6) + b"(({x!\x00")  # undecodable but framed
    await router.send_to(
        "Discord", ["tell", 5, "Foo", "bob", "Discord", "alice", "Bob", "still here"]
    )
    first = await handler.wait("tell")
    second = await handler.wait("tell")
    assert [first.payload[1], second.payload[1]] == ["hi", "still here"]
    await router.send_raw("Discord", struct.pack(">I", 1 << 20))
    await router.wait_for("startup-req-3")  # reconnected after the oversize header
    await client.wait_ready(timeout=5)
    await stop_client(client, task)


async def test_channel_fanout_and_own_echo_dropped(
    make_config: Callable[..., Config], store: StateStore, handler: RecordingHandler
) -> None:
    router = FakeRouter(echo_own_channel=True)
    await router.start()
    try:
        client = I3Client(make_config(router), store, handler)
        task = await run_client(client)
        await client.listen("intergossip", True)
        await router.wait_for("channel-listen")
        await client.send(pk.build_channel_m("Discord", "alice", "intergossip", "Alice", "hello"))
        await router.wait_for("channel-m")
        await asyncio.sleep(0.1)
        assert not [p for p in handler.packets if p.type == "channel-m"], "own echo must be dropped"
        await stop_client(client, task)
    finally:
        await router.stop()


async def test_send_when_disconnected_raises(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    client = I3Client(make_config(router), store, handler)
    with pytest.raises(NotConnectedError):
        await client.send(pk.build_who_req("Discord", "alice", "Foo"))


async def test_malformed_but_typed_packet_gets_bad_pkt_error(
    router: FakeRouter,
    make_config: Callable[..., Config],
    store: StateStore,
    handler: RecordingHandler,
) -> None:
    class Strict:
        async def handle(self, client: I3Client, pkt: pk.Packet) -> None:
            pk.parse_tell(pkt)

    client = I3Client(make_config(router), store, Strict())
    task = await run_client(client)
    await router.send_to(
        "Discord", ["tell", 5, "Foo", "bob", "Discord", "alice"]
    )  # missing payload
    err = await router.wait_for("error")
    assert err.target_mud == "Foo" and err.payload[0] == "bad-pkt"
    await stop_client(client, task)
