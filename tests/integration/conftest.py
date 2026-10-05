from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from i3bridge.config import Config, RouterConfig, config_from_dict
from i3bridge.i3.client import I3Client, PacketHandler
from i3bridge.i3.packets import Packet
from i3bridge.state.db import StateStore

from .fake_router import FakeRouter


class RecordingHandler:
    def __init__(self) -> None:
        self.packets: list[Packet] = []
        self.event = asyncio.Event()

    async def handle(self, client: I3Client, pkt: Packet) -> None:
        self.packets.append(pkt)
        self.event.set()

    async def wait(self, ptype: str, timeout: float = 5.0) -> Packet:
        async def _wait() -> Packet:
            while True:
                for p in self.packets:
                    if p.type == ptype:
                        self.packets.remove(p)
                        return p
                self.event.clear()
                await self.event.wait()

        return await asyncio.wait_for(_wait(), timeout)


@pytest.fixture
async def router() -> AsyncIterator[FakeRouter]:
    r = FakeRouter()
    await r.start()
    yield r
    await r.stop()


@pytest.fixture
def make_config() -> Callable[..., Config]:
    def _make(*routers: FakeRouter, **overrides: object) -> Config:
        cfg = config_from_dict(
            {
                "mud": {"name": "Discord"},
                "i3": {
                    "reconnect_min_seconds": 0.05,
                    "reconnect_max_seconds": 0.2,
                    "connect_timeout_seconds": 2,
                    "startup_timeout_seconds": 2,
                    "idle_reconnect_after_seconds": 0,
                    "keepalive_interval_seconds": 0,
                },
            }
        )
        cfg.routers = [RouterConfig(r.name, "127.0.0.1", r.port) for r in routers]
        for key, value in overrides.items():
            if key.startswith("i3_"):
                setattr(cfg.i3, key[3:], value)
        return cfg

    return _make


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


async def run_client(client: I3Client) -> asyncio.Task[None]:
    task = asyncio.create_task(client.run())
    await client.wait_ready(timeout=5)
    return task


async def stop_client(client: I3Client, task: asyncio.Task[None]) -> None:
    client.stop()
    await asyncio.wait_for(task, 5)


_: type[PacketHandler] = RecordingHandler
