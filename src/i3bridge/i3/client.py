"""The I3 router connection: handshake, state sync, reconnects, packet dispatch.

State machine::

    DISCONNECTED -> CONNECTING -> AWAIT_STARTUP_REPLY -> READY
          ^                                              |
          +------------- backoff <---- error/EOF/idle ---+

The client owns everything that talks to the router. Services register through a
:class:`PacketHandler`; the bridge observes lifecycle through the ``on_*`` callback lists.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..config import Config, RouterConfig
from ..log import PACKET_LOGGER
from ..state.db import StateStore
from . import packets as pk
from .framing import FramingError, frame, read_frame
from .lpc import LPCError, bytes_to_text, decode, encode

log = logging.getLogger(__name__)
plog = logging.getLogger(PACKET_LOGGER)

SERVICES: dict[str, int] = {
    "tell": 1,
    "emoteto": 1,
    "channel": 1,
    "who": 1,
    "finger": 1,
    "locate": 1,
    "chan-who-req": 1,
    "chan-who-reply": 1,
    "ucache": 1,
    "auth": 0,
}


class State(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AWAIT_STARTUP_REPLY = "await-startup-reply"
    READY = "ready"


class NotConnectedError(RuntimeError):
    pass


class PacketHandler(Protocol):
    async def handle(self, client: I3Client, pkt: pk.Packet) -> None: ...


class _Reconnect(Exception):
    """Internal: abandon the current connection and run the backoff loop."""

    def __init__(self, reason: str, *, fatal_for_router: bool = False) -> None:
        super().__init__(reason)
        self.fatal_for_router = fatal_for_router


Callback = Callable[[], Awaitable[None]]
ErrorCallback = Callable[[pk.ErrorPacket], Awaitable[None]]


@dataclass(slots=True)
class ClientStats:
    state: State = State.DISCONNECTED
    router: str | None = None
    connected_since: float | None = None
    last_rx: float | None = None
    last_tx: float | None = None
    packets_in: int = 0
    packets_out: int = 0
    reconnects: int = 0
    last_error: str | None = None
    listened_channels: set[str] = field(default_factory=set)


class I3Client:
    def __init__(self, cfg: Config, store: StateStore, handler: PacketHandler) -> None:
        self.cfg = cfg
        self.store = store
        self.handler = handler
        self.mud_name = cfg.mud.name
        self.stats = ClientStats()
        self.on_ready: list[Callback] = []
        self.on_disconnect: list[Callback] = []
        self.on_mudlist: list[Callback] = []
        self.on_chanlist: list[Callback] = []
        self.on_router_error: list[ErrorCallback] = []
        self._routers: list[RouterConfig] = list(cfg.routers)
        self._router_index = 0
        self._redirect: RouterConfig | None = None
        self._router: RouterConfig | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._reconnect_now = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._attempt = 0
        self._sent_zero_password = False
        self._requested_full_lists = False
        self._first_mudlist_pending = True
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ public API

    @property
    def state(self) -> State:
        return self.stats.state

    @property
    def ready(self) -> bool:
        return self.stats.state is State.READY

    @property
    def router_name(self) -> str:
        if self._router is None:
            raise NotConnectedError("not connected to a router")
        return self._router.name

    async def wait_ready(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await asyncio.wait_for(self._ready_event.wait(), timeout)

    def stop(self) -> None:
        self._stop.set()
        self._reconnect_now.set()

    def force_reconnect(self, reason: str = "requested") -> None:
        self.stats.last_error = reason
        self._reconnect_now.set()

    async def send(self, pkt: pk.Packet) -> None:
        """Encode and send one packet. Raises :class:`NotConnectedError` if no link is up."""
        writer = self._writer
        if writer is None or writer.is_closing():
            raise NotConnectedError("no router connection")
        text = encode(pkt.to_lpc())
        data = frame(text.encode(self.cfg.i3.outbound_encoding, errors="replace"))
        async with self._send_lock:
            writer.write(data)
            await writer.drain()
        self.stats.packets_out += 1
        self.stats.last_tx = time.monotonic()
        plog.debug(">> %s", text)

    async def listen(self, channel: str, on: bool) -> None:
        if on:
            self.stats.listened_channels.add(channel)
        else:
            self.stats.listened_channels.discard(channel)
        if self.ready:
            await self.send(pk.build_channel_listen(self.mud_name, self.router_name, channel, on))

    # --------------------------------------------------------------------- main loop

    async def run(self) -> None:
        """Connect and stay connected until :meth:`stop` is called."""
        try:
            while not self._stop.is_set():
                router = self._pick_router()
                try:
                    await self._session(router)
                except _Reconnect as exc:
                    self.stats.last_error = str(exc)
                    log.warning("connection to %s ended: %s", router.name, exc)
                    if exc.fatal_for_router:
                        self._router_index += 1
                except (OSError, asyncio.IncompleteReadError, FramingError, TimeoutError) as exc:
                    self.stats.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("connection to %s failed: %s", router.name, self.stats.last_error)
                    self._router_index += 1
                finally:
                    await self._teardown()
                if self._stop.is_set():
                    break
                await self._backoff()
        finally:
            await self._teardown()

    def _pick_router(self) -> RouterConfig:
        if self._redirect is not None:
            router, self._redirect = self._redirect, None
            return router
        return self._routers[self._router_index % len(self._routers)]

    async def _backoff(self) -> None:
        self._attempt += 1
        base = min(
            self.cfg.i3.reconnect_max_seconds,
            self.cfg.i3.reconnect_min_seconds * (2 ** min(self._attempt - 1, 12)),
        )
        delay = base * random.uniform(0.8, 1.2)
        log.info("reconnecting in %.1fs (attempt %d)", delay, self._attempt)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), delay)

    async def _session(self, router: RouterConfig) -> None:
        self._router = router
        self._reconnect_now.clear()
        self._set_state(State.CONNECTING)
        log.info("connecting to %s at %s:%d", router.name, router.host, router.port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(router.host, router.port), self.cfg.i3.connect_timeout_seconds
        )
        _enable_keepalive(writer)
        self._writer = writer
        self.stats.router = router.name
        self.stats.connected_since = time.monotonic()
        self.stats.last_rx = time.monotonic()

        await self._handshake(reader, router)

        self._set_state(State.READY)
        self._ready_event.set()
        self.stats.reconnects += 1 if self._attempt else 0
        connected_at = time.monotonic()
        for cb in self.on_ready:
            await _safe(cb())
        for channel in sorted(self.stats.listened_channels):
            await self.send(pk.build_channel_listen(self.mud_name, router.name, channel, True))

        pump = asyncio.create_task(self._pump(reader), name="i3-pump")
        watchdog = asyncio.create_task(self._watchdog(), name="i3-watchdog")
        waiter = asyncio.create_task(self._wait_reconnect(), name="i3-reconnect-waiter")
        self._tasks.update({pump, watchdog, waiter})
        try:
            done, _ = await asyncio.wait(
                {pump, watchdog, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if time.monotonic() - connected_at > 60:
                self._attempt = 0  # a healthy session resets the backoff
            if self._stop.is_set():
                await self._send_shutdown()
                return
            for task in done:
                if task is not waiter:
                    task.result()  # re-raise pump/watchdog exceptions
            raise _Reconnect(self.stats.last_error or "reconnect requested")
        finally:
            for task in (pump, watchdog, waiter):
                task.cancel()
            for task in (pump, watchdog, waiter):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._tasks.difference_update({pump, watchdog, waiter})

    async def _wait_reconnect(self) -> None:
        await self._reconnect_now.wait()

    async def _handshake(self, reader: asyncio.StreamReader, router: RouterConfig) -> None:
        self._set_state(State.AWAIT_STARTUP_REPLY)
        self._first_mudlist_pending = True
        password = self.store.get_password(self.mud_name, router.name)
        if password is None:
            password = self.store.any_password(self.mud_name)
            if password is not None:
                log.info(
                    "no password for %s yet; trying the one issued by another router", router.name
                )
        self._sent_zero_password = password is None
        mudlist_id, chanlist_id = (
            (0, 0) if password is None else self.store.get_list_ids(self.mud_name, router.name)
        )
        self._requested_full_lists = mudlist_id == 0
        m = self.cfg.mud
        await self.send(
            pk.build_startup_req(
                mud=self.mud_name,
                router=router.name,
                password=password or 0,
                old_mudlist_id=mudlist_id,
                old_chanlist_id=chanlist_id,
                player_port=m.player_port,
                tcp_port=m.tcp_port,
                udp_port=m.udp_port,
                mudlib=m.mudlib,
                base_mudlib=m.base_mudlib,
                driver=m.driver,
                mud_type=m.mud_type,
                open_status=m.open_status,
                admin_email=m.admin_email,
                services=SERVICES,
            )
        )
        deadline = time.monotonic() + self.cfg.i3.startup_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Reconnect("timed out waiting for startup-reply")
            pkt = await asyncio.wait_for(self._read_packet(reader), remaining)
            if pkt is None:
                continue
            if pkt.type == "startup-reply":
                reply = pk.parse_startup_reply(pkt)
                self.store.set_password(self.mud_name, router.name, reply.password)
                log.info(
                    "startup-reply from %s; password stored; %d routers listed",
                    router.name,
                    len(reply.router_list),
                )
                if reply.router_list and reply.router_list[0].name != router.name:
                    preferred = reply.router_list[0]
                    if preferred.host and preferred.port:
                        self._redirect = RouterConfig(
                            preferred.name, preferred.host, preferred.port
                        )
                        raise _Reconnect(
                            f"router asked us to use {preferred.name} "
                            f"at {preferred.host}:{preferred.port}"
                        )
                return
            if pkt.type == "error":
                err = pk.parse_error(pkt)
                log.error("router %s rejected startup: %s: %s", router.name, err.code, err.message)
                for ecb in self.on_router_error:
                    await _safe(ecb(err))
                raise _Reconnect(
                    f"startup rejected: {err.code}: {err.message}", fatal_for_router=True
                )
            # Some routers send mudlist/chanlist before the reply; keep them.
            await self._dispatch(pkt)

    async def _pump(self, reader: asyncio.StreamReader) -> None:
        while True:
            pkt = await self._read_packet(reader)
            if pkt is not None:
                await self._dispatch(pkt)

    async def _read_packet(self, reader: asyncio.StreamReader) -> pk.Packet | None:
        raw = await read_frame(reader, self.cfg.i3.max_packet_bytes)
        self.stats.last_rx = time.monotonic()
        self.stats.packets_in += 1
        text = bytes_to_text(raw)
        plog.debug("<< %s", text)
        try:
            return pk.Packet.from_lpc(decode(text))
        except (LPCError, pk.PacketError) as exc:
            log.warning("dropping undecodable packet (%s): %.200r", exc, text)
            return None

    async def _dispatch(self, pkt: pk.Packet) -> None:
        if pkt.orig_mud == self.mud_name and pkt.type not in ("startup-reply",):
            return  # never react to our own traffic echoed back
        try:
            if pkt.type == "mudlist":
                mudlist_id, mud_entries = pk.parse_mudlist(pkt)
                self.store.apply_mudlist(mud_entries, replace=self._requested_full_lists)
                self._requested_full_lists = False
                self.store.set_list_ids(self.mud_name, self.router_name, mudlist_id=mudlist_id)
                if self._first_mudlist_pending:
                    # *dalet delivers the list in chunks of ten; summarise once it settles
                    self._first_mudlist_pending = False
                    asyncio.get_running_loop().call_later(5.0, self._log_mudlist_summary)
                log.debug("mudlist %d: %d entries updated", mudlist_id, len(mud_entries))
                for cb in self.on_mudlist:
                    await _safe(cb())
            elif pkt.type == "chanlist-reply":
                chanlist_id, chan_entries = pk.parse_chanlist(pkt)
                self.store.apply_chanlist(
                    chan_entries,
                    replace=self.store.get_list_ids(self.mud_name, self.router_name)[1] == 0,
                )
                self.store.set_list_ids(self.mud_name, self.router_name, chanlist_id=chanlist_id)
                log.info(
                    "chanlist %d: %d entries updated, %d channels known",
                    chanlist_id,
                    len(chan_entries),
                    len(self.store.get_chanlist()),
                )
                for cb in self.on_chanlist:
                    await _safe(cb())
            elif pkt.type == "error" and pkt.target_user is None:
                err = pk.parse_error(pkt)
                log.warning("error from %s: %s: %s", pkt.orig_mud, err.code, err.message)
                for ecb in self.on_router_error:
                    await _safe(ecb(err))
            else:
                await self.handler.handle(self, pkt)
        except pk.PacketError as exc:
            log.warning("malformed %s packet from %s: %s", pkt.type, pkt.orig_mud, exc)
            if pkt.orig_mud and not pkt.orig_mud.startswith("*"):
                with contextlib.suppress(NotConnectedError, OSError):
                    await self.send(
                        pk.build_error(
                            self.mud_name,
                            pkt.orig_mud,
                            pkt.orig_user,
                            "bad-pkt",
                            str(exc),
                            pkt.to_lpc(),
                        )
                    )
        except Exception:
            log.exception("handler failed for %s packet from %s", pkt.type, pkt.orig_mud)

    def _log_mudlist_summary(self) -> None:
        muds = self.store.get_mudlist()
        log.info(
            "mudlist synced: %d muds known, %d up", len(muds), sum(1 for m in muds.values() if m.up)
        )

    async def _watchdog(self) -> None:
        idle_limit = self.cfg.i3.idle_reconnect_after_seconds
        keepalive = self.cfg.i3.keepalive_interval_seconds
        while True:
            await asyncio.sleep(min(30.0, max(1.0, keepalive / 4 if keepalive else 30.0)))
            now = time.monotonic()
            if idle_limit and self.stats.last_rx and now - self.stats.last_rx > idle_limit:
                self.stats.last_error = f"no traffic for {idle_limit:.0f}s"
                raise _Reconnect(self.stats.last_error)
            if keepalive and self.stats.last_tx and now - self.stats.last_tx > keepalive:
                channels = sorted(self.stats.listened_channels)
                if channels:
                    await self.send(
                        pk.build_channel_listen(self.mud_name, self.router_name, channels[0], True)
                    )

    async def _send_shutdown(self) -> None:
        if self._router is None:
            return
        with contextlib.suppress(NotConnectedError, OSError):
            await self.send(pk.build_shutdown(self.mud_name, self._router.name, 0))

    async def _teardown(self) -> None:
        writer, self._writer = self._writer, None
        was_ready = self.stats.state is State.READY
        self._ready_event.clear()
        self._set_state(State.DISCONNECTED)
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 5)
        if was_ready:
            for cb in self.on_disconnect:
                await _safe(cb())

    def _set_state(self, state: State) -> None:
        if self.stats.state is not state:
            log.debug("state %s -> %s", self.stats.state.value, state.value)
        self.stats.state = state


async def _safe(coro: Awaitable[None]) -> None:
    try:
        await coro
    except Exception:
        log.exception("callback failed")


def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 120)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 4)
