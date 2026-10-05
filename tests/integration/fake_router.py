"""A small in-process InterMUD-3 router for tests.

It implements enough of the router side to exercise the client: password assignment and
checking, mudlist/chanlist push with ids, channel-listen bookkeeping and channel fan-out,
routing of targeted packets, locate broadcast, error replies, plus hooks to inject raw
bytes, disconnect a mud, and await packets of a given type.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections import defaultdict
from dataclasses import dataclass, field

from i3bridge.i3.framing import FrameParser, frame
from i3bridge.i3.lpc import LPCValue, bytes_to_text, decode, encode
from i3bridge.i3.packets import Packet


@dataclass
class Registry:
    """Shared between routers to emulate an irn-linked network."""

    passwords: dict[str, int] = field(default_factory=dict)


@dataclass
class _Conn:
    name: str | None
    writer: asyncio.StreamWriter
    parser: FrameParser = field(default_factory=FrameParser)


class FakeRouter:
    def __init__(
        self,
        name: str = "*fake",
        *,
        registry: Registry | None = None,
        router_list: list[tuple[str, str]] | None = None,
        echo_own_channel: bool = False,
        accept_unknown_password: bool = True,
        chanlist: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        self.name = name
        self.registry = registry or Registry()
        self.router_list = router_list
        self.echo_own_channel = echo_own_channel
        self.accept_unknown_password = accept_unknown_password
        self.mudlist: dict[str, list[LPCValue]] = {}
        self.mudlist_id = random.randint(1000, 9999)
        self.chanlist: dict[str, tuple[str, int]] = chanlist or {
            "intergossip": (name, 0),
            "dchat": ("Dead Souls", 1),
        }
        self.chanlist_id = random.randint(1000, 9999)
        self.listens: dict[str, set[str]] = defaultdict(set)
        self.conns: dict[str, _Conn] = {}
        self.received: list[Packet] = []
        self._waiters: dict[str, list[asyncio.Future[Packet]]] = defaultdict(list)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ lifecycle

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._serve, host, port)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self.conns.values()):
            conn.writer.close()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.conns.clear()

    # ------------------------------------------------------------------- test hooks

    async def wait_for(self, ptype: str, timeout: float = 5.0) -> Packet:
        for p in self.received:
            if p.type == ptype:
                self.received.remove(p)
                return p
        fut: asyncio.Future[Packet] = asyncio.get_running_loop().create_future()
        self._waiters[ptype].append(fut)
        return await asyncio.wait_for(fut, timeout)

    async def send_to(self, mud: str, pkt: Packet | list[LPCValue]) -> None:
        lpc = pkt.to_lpc() if isinstance(pkt, Packet) else pkt
        await self._write(self.conns[mud], encode(lpc).encode())

    async def send_raw(self, mud: str, data: bytes) -> None:
        conn = self.conns[mud]
        conn.writer.write(data)
        await conn.writer.drain()

    async def disconnect(self, mud: str) -> None:
        conn = self.conns.pop(mud, None)
        if conn is not None:
            conn.writer.close()
            with contextlib.suppress(Exception):
                await conn.writer.wait_closed()

    def connected(self, mud: str) -> bool:
        return mud in self.conns

    async def push_mudlist_delta(self, entries: dict[str, list[LPCValue] | None]) -> None:
        for name, info in entries.items():
            if info is None:
                self.mudlist.pop(name, None)
            else:
                self.mudlist[name] = info
        self.mudlist_id += 1
        for conn in list(self.conns.values()):
            if conn.name:
                await self._send(
                    conn,
                    ["mudlist", 5, self.name, 0, conn.name, 0, self.mudlist_id, _mapping(entries)],
                )

    async def push_chanlist_delta(self, entries: dict[str, tuple[str, int] | None]) -> None:
        for name, info in entries.items():
            if info is None:
                self.chanlist.pop(name, None)
            else:
                self.chanlist[name] = info
        self.chanlist_id += 1
        payload: dict[int | float | str, LPCValue] = {
            k: (0 if v is None else [v[0], v[1]]) for k, v in entries.items()
        }
        for conn in list(self.conns.values()):
            if conn.name:
                await self._send(
                    conn,
                    ["chanlist-reply", 5, self.name, 0, conn.name, 0, self.chanlist_id, payload],
                )

    # --------------------------------------------------------------------- serving

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = _Conn(None, writer)
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for payload in conn.parser.feed(data):
                    try:
                        pkt = Packet.from_lpc(decode(bytes_to_text(payload)))
                    except Exception:
                        await self._send(
                            conn,
                            [
                                "error",
                                5,
                                self.name,
                                0,
                                conn.name or 0,
                                0,
                                "bad-pkt",
                                "unparseable",
                                0,
                            ],
                        )
                        continue
                    await self._handle(conn, pkt)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if conn.name and self.conns.get(conn.name) is conn:
                del self.conns[conn.name]
                if conn.name in self.mudlist:
                    self.mudlist[conn.name][0] = 0
            writer.close()
            self._tasks.discard(task)

    async def _handle(self, conn: _Conn, pkt: Packet) -> None:
        self.received.append(pkt)
        waiters = self._waiters.get(pkt.type)
        if waiters:
            fut = waiters.pop(0)
            if not fut.done():
                fut.set_result(pkt)
                self.received.remove(pkt)

        if pkt.type == "startup-req-3":
            await self._startup(conn, pkt)
            return
        if conn.name is None:
            await self._send(
                conn,
                [
                    "error",
                    5,
                    self.name,
                    0,
                    pkt.orig_mud,
                    0,
                    "unk-src",
                    "startup first",
                    pkt.to_lpc(),
                ],
            )
            return
        mud = conn.name
        t = pkt.type
        if t == "shutdown":
            if mud in self.mudlist:
                self.mudlist[mud][0] = (
                    int(pkt.payload[0]) if pkt.payload and isinstance(pkt.payload[0], int) else 0
                )
        elif t == "channel-listen":
            channel = str(pkt.payload[0])
            if channel not in self.chanlist:
                await self._error(conn, pkt, "unk-channel", f"no channel {channel}")
                return
            (self.listens[channel].add if pkt.payload[1] else self.listens[channel].discard)(mud)
        elif t in ("channel-m", "channel-e", "channel-t"):
            channel = str(pkt.payload[0])
            if channel not in self.chanlist:
                await self._error(conn, pkt, "unk-channel", f"no channel {channel}")
                return
            for listener in list(self.listens[channel]):
                if listener == mud and not self.echo_own_channel:
                    continue
                target = self.conns.get(listener)
                if target is not None:
                    await self._send(target, pkt.to_lpc())
        elif t == "locate-req":
            for other in list(self.conns.values()):
                if other is not conn and other.name:
                    await self._send(other, pkt.to_lpc())
        elif t in ("channel-add", "channel-remove", "channel-admin"):
            await self._error(conn, pkt, "not-imp", "fake router does not host channels")
        elif pkt.target_mud is not None:
            target = self.conns.get(pkt.target_mud)
            if target is None:
                await self._error(conn, pkt, "unk-dst", f"unknown mud {pkt.target_mud}")
            else:
                await self._send(target, pkt.to_lpc())
        else:
            await self._error(conn, pkt, "unk-type", f"unknown packet type {t}")

    async def _startup(self, conn: _Conn, pkt: Packet) -> None:
        mud = pkt.orig_mud
        password = int(pkt.payload[0]) if isinstance(pkt.payload[0], int) else 0
        old_mudlist_id = int(pkt.payload[1]) if isinstance(pkt.payload[1], int) else 0
        old_chanlist_id = int(pkt.payload[2]) if isinstance(pkt.payload[2], int) else 0
        known = self.registry.passwords.get(mud)
        if known is not None and password != known:
            await self._send(
                conn,
                ["error", 5, self.name, 0, mud, 0, "not-allowed", "bad password", pkt.to_lpc()],
            )
            conn.writer.close()
            return
        if known is None:
            if password and not self.accept_unknown_password:
                await self._send(
                    conn,
                    [
                        "error",
                        5,
                        self.name,
                        0,
                        mud,
                        0,
                        "not-allowed",
                        "unknown mud with password",
                        pkt.to_lpc(),
                    ],
                )
                conn.writer.close()
                return
            known = password or random.randint(1, 2**31 - 1)
            self.registry.passwords[mud] = known
        old = self.conns.get(mud)
        if old is not None and old is not conn:
            old.writer.close()
        conn.name = mud
        self.conns[mud] = conn
        info: list[LPCValue] = [
            -1,
            "127.0.0.1",
            *pkt.payload[3:6],
            *pkt.payload[6:12],
            pkt.payload[12],
            pkt.payload[13],
        ]
        self.mudlist[mud] = info
        self.mudlist_id += 1
        routers = self.router_list or [(self.name, f"127.0.0.1 {self.port}")]
        await self._send(
            conn, ["startup-reply", 5, self.name, 0, mud, 0, [[n, a] for n, a in routers], known]
        )
        if old_mudlist_id != self.mudlist_id - 1:
            await self._send(
                conn, ["mudlist", 5, self.name, 0, mud, 0, self.mudlist_id, _mapping(self.mudlist)]
            )
        else:
            await self._send(
                conn, ["mudlist", 5, self.name, 0, mud, 0, self.mudlist_id, {mud: info}]
            )
        if old_chanlist_id != self.chanlist_id:
            payload: dict[int | float | str, LPCValue] = {
                k: [v[0], v[1]] for k, v in self.chanlist.items()
            }
            await self._send(
                conn, ["chanlist-reply", 5, self.name, 0, mud, 0, self.chanlist_id, payload]
            )
        for other in list(self.conns.values()):
            if other is not conn and other.name:
                await self._send(
                    other, ["mudlist", 5, self.name, 0, other.name, 0, self.mudlist_id, {mud: info}]
                )

    async def _error(self, conn: _Conn, pkt: Packet, code: str, message: str) -> None:
        await self._send(
            conn,
            [
                "error",
                5,
                self.name,
                0,
                conn.name or 0,
                pkt.orig_user or 0,
                code,
                message,
                pkt.to_lpc(),
            ],
        )

    async def _send(self, conn: _Conn, lpc: list[LPCValue]) -> None:
        await self._write(conn, encode(lpc).encode())

    @staticmethod
    async def _write(conn: _Conn, payload: bytes) -> None:
        if conn.writer.is_closing():
            return
        conn.writer.write(frame(payload))
        with contextlib.suppress(ConnectionError):
            await conn.writer.drain()


def _mapping(entries: dict[str, list[LPCValue] | None]) -> dict[int | float | str, LPCValue]:
    return {k: (0 if v is None else list(v)) for k, v in entries.items()}
