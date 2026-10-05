"""Typed views over InterMUD-3 packets.

Every packet is an LPC array ``({ type, ttl, orig_mud, orig_user, target_mud, target_user,
...payload })``. :class:`Packet` holds the common header plus the raw payload; the parse_*
functions give typed payloads for the packets the bridge handles, and the build_* functions
construct outbound packets exactly as the specification lays them out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .lpc import LPCKey, LPCValue

log = logging.getLogger(__name__)

TTL = 5


class PacketError(ValueError):
    """The LPC value is not a well-formed packet of the expected shape."""


def _opt_str(value: LPCValue) -> str | None:
    if value is None or value == 0:
        return None
    if isinstance(value, str):
        return value
    raise PacketError(f"expected string or 0, got {type(value).__name__}")


def _str(value: LPCValue, what: str) -> str:
    if isinstance(value, str):
        return value
    if value == 0 or value is None:
        return ""
    raise PacketError(f"{what}: expected string, got {type(value).__name__}")


def _int(value: LPCValue, what: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return 0
    raise PacketError(f"{what}: expected integer, got {type(value).__name__}")


def _list(value: LPCValue, what: str) -> list[LPCValue]:
    if isinstance(value, list):
        return value
    if value == 0 or value is None:
        return []
    raise PacketError(f"{what}: expected array, got {type(value).__name__}")


def _dict(value: LPCValue, what: str) -> dict[int | float | str, LPCValue]:
    if isinstance(value, dict):
        return value
    if value == 0 or value is None:
        return {}
    raise PacketError(f"{what}: expected mapping, got {type(value).__name__}")


def _flag(value: LPCValue) -> int:
    """A services-mapping value: 1/0, or a port number for OOB services. Muds in the wild
    send strings here too, so be generous: numeric strings become numbers, anything else
    non-empty counts as supported."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            return 1
    return 1 if value else 0


def _need(payload: list[LPCValue], n: int, what: str) -> None:
    if len(payload) < n:
        raise PacketError(f"{what}: expected {n} payload fields, got {len(payload)}")


@dataclass(slots=True)
class Packet:
    type: str
    orig_mud: str
    orig_user: str | None
    target_mud: str | None
    target_user: str | None
    payload: list[LPCValue] = field(default_factory=list)
    ttl: int = TTL

    def to_lpc(self) -> list[LPCValue]:
        return [
            self.type,
            self.ttl,
            self.orig_mud,
            self.orig_user or 0,
            self.target_mud or 0,
            self.target_user or 0,
            *self.payload,
        ]

    @classmethod
    def from_lpc(cls, value: LPCValue) -> Packet:
        if not isinstance(value, list) or len(value) < 6:
            raise PacketError("packet must be an array of at least 6 elements")
        ptype, ttl, omud, ouser, tmud, tuser = value[:6]
        if not isinstance(ptype, str) or not ptype:
            raise PacketError("packet type must be a non-empty string")
        return cls(
            type=ptype,
            orig_mud=_str(omud, "orig_mud"),
            orig_user=_opt_str(ouser),
            target_mud=_opt_str(tmud),
            target_user=_opt_str(tuser),
            payload=list(value[6:]),
            ttl=_int(ttl, "ttl"),
        )


# ----------------------------------------------------------------------- typed payloads


@dataclass(slots=True, frozen=True)
class RouterEntry:
    name: str
    host: str
    port: int


@dataclass(slots=True, frozen=True)
class StartupReply:
    router_list: list[RouterEntry]
    password: int


def parse_startup_reply(p: Packet) -> StartupReply:
    _need(p.payload, 2, "startup-reply")
    routers: list[RouterEntry] = []
    for entry in _list(p.payload[0], "router_list"):
        items = _list(entry, "router entry")
        if len(items) < 2:
            raise PacketError("router entry needs name and address")
        name = _str(items[0], "router name")
        host, _, port = _str(items[1], "router address").partition(" ")
        try:
            routers.append(RouterEntry(name, host, int(port) if port else 0))
        except ValueError as exc:
            raise PacketError(f"bad router port {port!r}") from exc
    return StartupReply(routers, _int(p.payload[1], "password"))


@dataclass(slots=True, frozen=True)
class MudInfo:
    state: int  # -1 up, 0 down, n = down for n seconds
    ip: str
    player_port: int
    tcp_port: int
    udp_port: int
    mudlib: str
    base_mudlib: str
    driver: str
    mud_type: str
    open_status: str
    admin_email: str
    services: dict[str, int]
    other_data: dict[str, LPCValue]

    @property
    def up(self) -> bool:
        return self.state == -1


def parse_mud_info(value: LPCValue) -> MudInfo:
    items = _list(value, "mud info")
    if len(items) < 13:
        items = items + [0] * (13 - len(items))
    services_raw = _dict(items[11], "services")
    other_raw = _dict(items[12], "other_data")
    return MudInfo(
        state=_int(items[0], "state"),
        ip=_str(items[1], "ip"),
        player_port=_int(items[2], "player_port"),
        tcp_port=_int(items[3], "tcp_port"),
        udp_port=_int(items[4], "udp_port"),
        mudlib=_str(items[5], "mudlib"),
        base_mudlib=_str(items[6], "base_mudlib"),
        driver=_str(items[7], "driver"),
        mud_type=_str(items[8], "mud_type"),
        open_status=_str(items[9], "open_status"),
        admin_email=_str(items[10], "admin_email"),
        services={str(k): _flag(v) for k, v in services_raw.items()},
        other_data={str(k): v for k, v in other_raw.items()},
    )


def parse_mudlist(p: Packet) -> tuple[int, dict[str, MudInfo | None]]:
    """Return ``(mudlist_id, {mudname: info-or-None})``; ``None`` means deleted."""
    _need(p.payload, 2, "mudlist")
    entries: dict[str, MudInfo | None] = {}
    for name, info in _dict(p.payload[1], "mudlist").items():
        if info == 0 or info is None:
            entries[str(name)] = None
            continue
        try:
            entries[str(name)] = parse_mud_info(info)
        except PacketError as exc:
            # one odd mud must not cost us the whole list
            log.warning("skipping unparseable mudlist entry for %r: %s", name, exc)
    return _int(p.payload[0], "mudlist_id"), entries


@dataclass(slots=True, frozen=True)
class ChannelInfo:
    host_mud: str
    type: int  # 0 selectively banned, 1 selectively admitted, 2 filtered


def parse_chanlist(p: Packet) -> tuple[int, dict[str, ChannelInfo | None]]:
    _need(p.payload, 2, "chanlist-reply")
    entries: dict[str, ChannelInfo | None] = {}
    for name, info in _dict(p.payload[1], "chanlist").items():
        if info == 0 or info is None:
            entries[str(name)] = None
            continue
        try:
            items = _list(info, "channel info")
            if len(items) < 2:
                raise PacketError("malformed info")
            entries[str(name)] = ChannelInfo(_str(items[0], "host_mud"), _int(items[1], "type"))
        except PacketError as exc:
            log.warning("skipping unparseable chanlist entry for %r: %s", name, exc)
    return _int(p.payload[0], "chanlist_id"), entries


@dataclass(slots=True, frozen=True)
class ChannelMessage:
    """``channel-m`` (message) or ``channel-e`` (emote with ``$N``)."""

    channel: str
    visname: str
    message: str


def parse_channel_message(p: Packet) -> ChannelMessage:
    _need(p.payload, 3, p.type)
    return ChannelMessage(
        _str(p.payload[0], "channel"), _str(p.payload[1], "visname"), _str(p.payload[2], "message")
    )


@dataclass(slots=True, frozen=True)
class ChannelTargetedEmote:
    channel: str
    target_mud: str
    target_user: str
    message_others: str
    message_target: str
    orig_visname: str
    target_visname: str


def parse_channel_t(p: Packet) -> ChannelTargetedEmote:
    _need(p.payload, 7, "channel-t")
    f = p.payload
    return ChannelTargetedEmote(
        _str(f[0], "channel"),
        _str(f[1], "target_mud"),
        _str(f[2], "target_user"),
        _str(f[3], "message_others"),
        _str(f[4], "message_target"),
        _str(f[5], "orig_visname"),
        _str(f[6], "target_visname"),
    )


@dataclass(slots=True, frozen=True)
class Tell:
    """``tell`` or ``emoteto``."""

    visname: str
    message: str


def parse_tell(p: Packet) -> Tell:
    _need(p.payload, 2, p.type)
    return Tell(_str(p.payload[0], "visname"), _str(p.payload[1], "message"))


@dataclass(slots=True, frozen=True)
class WhoEntry:
    visname: str
    idle: int
    extra: str


def parse_who_reply(p: Packet) -> list[WhoEntry]:
    _need(p.payload, 1, "who-reply")
    out: list[WhoEntry] = []
    for row in _list(p.payload[0], "who data"):
        items = _list(row, "who row")
        if not items:
            continue
        items = items + [0] * (3 - len(items))
        out.append(
            WhoEntry(_str(items[0], "visname"), _int(items[1], "idle"), _str(items[2], "extra"))
        )
    return out


@dataclass(slots=True, frozen=True)
class FingerReply:
    visname: str
    title: str
    real_name: str
    email: str
    loginout_time: str
    idle: int
    ip_name: str
    level: str
    extra: str


def parse_finger_reply(p: Packet) -> FingerReply:
    f = p.payload + [0] * (9 - len(p.payload))
    return FingerReply(
        _str(f[0], "visname"),
        _str(f[1], "title"),
        _str(f[2], "real_name"),
        _str(f[3], "email"),
        _str(f[4], "loginout_time"),
        _int(f[5], "idle"),
        _str(f[6], "ip_name"),
        str(f[7]) if f[7] not in (0, None) else "",
        _str(f[8], "extra"),
    )


@dataclass(slots=True, frozen=True)
class LocateReply:
    located_mud: str
    located_visname: str
    idle: int
    status: str


def parse_locate_reply(p: Packet) -> LocateReply:
    f = p.payload + [0] * (4 - len(p.payload))
    return LocateReply(
        _str(f[0], "located_mud"),
        _str(f[1], "located_visname"),
        _int(f[2], "idle"),
        _str(f[3], "status"),
    )


@dataclass(slots=True, frozen=True)
class ChanWhoReply:
    channel: str
    users: list[str]


def parse_chan_who_reply(p: Packet) -> ChanWhoReply:
    _need(p.payload, 2, "chan-who-reply")
    return ChanWhoReply(
        _str(p.payload[0], "channel"),
        [_str(u, "user") for u in _list(p.payload[1], "users")],
    )


@dataclass(slots=True, frozen=True)
class UcacheUpdate:
    username: str
    visname: str
    gender: int  # 0 male, 1 female, 2 neuter


def parse_ucache_update(p: Packet) -> UcacheUpdate:
    _need(p.payload, 3, "ucache-update")
    return UcacheUpdate(
        _str(p.payload[0], "username"), _str(p.payload[1], "visname"), _int(p.payload[2], "gender")
    )


@dataclass(slots=True, frozen=True)
class ErrorPacket:
    code: str
    message: str
    bad_packet: LPCValue


def parse_error(p: Packet) -> ErrorPacket:
    f = p.payload + [0] * (3 - len(p.payload))
    return ErrorPacket(_str(f[0], "code"), _str(f[1], "message"), f[2])


# ----------------------------------------------------------------------------- builders

ERROR_CODES = frozenset(
    {
        "unk-dst",
        "not-imp",
        "unk-type",
        "unk-src",
        "bad-pkt",
        "bad-proto",
        "not-allowed",
        "unk-user",
        "unk-channel",
    }
)


def build_startup_req(
    *,
    mud: str,
    router: str,
    password: int,
    old_mudlist_id: int,
    old_chanlist_id: int,
    player_port: int,
    tcp_port: int,
    udp_port: int,
    mudlib: str,
    base_mudlib: str,
    driver: str,
    mud_type: str,
    open_status: str,
    admin_email: str,
    services: dict[str, int],
    other_data: dict[str, LPCValue] | None = None,
) -> Packet:
    services_lpc: dict[LPCKey, LPCValue] = {k: v for k, v in services.items()}
    other_lpc: dict[LPCKey, LPCValue] = {k: v for k, v in (other_data or {}).items()}
    return Packet(
        "startup-req-3",
        mud,
        None,
        router,
        None,
        [
            password,
            old_mudlist_id,
            old_chanlist_id,
            player_port,
            tcp_port,
            udp_port,
            mudlib,
            base_mudlib,
            driver,
            mud_type,
            open_status,
            admin_email,
            services_lpc,
            other_lpc or 0,
        ],
    )


def build_shutdown(mud: str, router: str, restart_delay: int = 0) -> Packet:
    return Packet("shutdown", mud, None, router, None, [restart_delay])


def build_error(
    mud: str,
    target_mud: str,
    target_user: str | None,
    code: str,
    message: str,
    bad_packet: LPCValue = 0,
) -> Packet:
    return Packet("error", mud, None, target_mud, target_user, [code, message, bad_packet])


def build_channel_listen(mud: str, router: str, channel: str, on: bool) -> Packet:
    return Packet("channel-listen", mud, None, router, None, [channel, 1 if on else 0])


def build_channel_m(mud: str, user: str, channel: str, visname: str, message: str) -> Packet:
    return Packet("channel-m", mud, user, None, None, [channel, visname, message])


def build_channel_e(mud: str, user: str, channel: str, visname: str, message: str) -> Packet:
    return Packet("channel-e", mud, user, None, None, [channel, visname, message])


def build_channel_t(
    mud: str,
    user: str,
    channel: str,
    target_mud: str,
    target_user: str,
    message_others: str,
    message_target: str,
    orig_visname: str,
    target_visname: str,
) -> Packet:
    return Packet(
        "channel-t",
        mud,
        user,
        None,
        None,
        [
            channel,
            target_mud,
            target_user,
            message_others,
            message_target,
            orig_visname,
            target_visname,
        ],
    )


def build_channel_add(mud: str, user: str, router: str, channel: str, channel_type: int) -> Packet:
    return Packet("channel-add", mud, user, router, None, [channel, channel_type])


def build_channel_remove(mud: str, user: str, router: str, channel: str) -> Packet:
    return Packet("channel-remove", mud, user, router, None, [channel])


def build_channel_admin(
    mud: str, user: str, router: str, channel: str, add_muds: list[str], remove_muds: list[str]
) -> Packet:
    return Packet(
        "channel-admin", mud, user, router, None, [channel, list(add_muds), list(remove_muds)]
    )


def build_chan_who_req(mud: str, user: str, target_mud: str, channel: str) -> Packet:
    return Packet("chan-who-req", mud, user, target_mud, None, [channel])


def build_chan_who_reply(
    mud: str, target_mud: str, target_user: str | None, channel: str, users: list[str]
) -> Packet:
    return Packet("chan-who-reply", mud, None, target_mud, target_user, [channel, list(users)])


def build_chan_user_req(mud: str, target_mud: str, username: str) -> Packet:
    return Packet("chan-user-req", mud, None, target_mud, None, [username])


def build_chan_user_reply(
    mud: str, target_mud: str, username: str, visname: str, gender: int
) -> Packet:
    return Packet("chan-user-reply", mud, None, target_mud, None, [username, visname, gender])


def build_tell(
    mud: str, user: str, target_mud: str, target_user: str, visname: str, message: str
) -> Packet:
    return Packet("tell", mud, user, target_mud, target_user, [visname, message])


def build_emoteto(
    mud: str, user: str, target_mud: str, target_user: str, visname: str, message: str
) -> Packet:
    return Packet("emoteto", mud, user, target_mud, target_user, [visname, message])


def build_who_req(mud: str, user: str, target_mud: str) -> Packet:
    return Packet("who-req", mud, user, target_mud, None, [])


def build_who_reply(
    mud: str, target_mud: str, target_user: str | None, entries: list[WhoEntry]
) -> Packet:
    rows: list[LPCValue] = [[e.visname, e.idle, e.extra] for e in entries]
    return Packet("who-reply", mud, None, target_mud, target_user, [rows])


def build_finger_req(mud: str, user: str, target_mud: str, username: str) -> Packet:
    return Packet("finger-req", mud, user, target_mud, None, [username])


def build_finger_reply(
    mud: str, target_mud: str, target_user: str | None, reply: FingerReply
) -> Packet:
    return Packet(
        "finger-reply",
        mud,
        None,
        target_mud,
        target_user,
        [
            reply.visname,
            reply.title,
            reply.real_name or 0,
            reply.email or 0,
            reply.loginout_time,
            reply.idle,
            reply.ip_name or 0,
            reply.level or 0,
            reply.extra or 0,
        ],
    )


def build_locate_req(mud: str, user: str, username: str) -> Packet:
    return Packet("locate-req", mud, user, None, None, [username])


def build_locate_reply(
    mud: str,
    target_mud: str,
    target_user: str | None,
    located_mud: str,
    located_visname: str,
    idle: int,
    status: str | None,
) -> Packet:
    return Packet(
        "locate-reply",
        mud,
        None,
        target_mud,
        target_user,
        [located_mud, located_visname, idle, status or 0],
    )


def build_ucache_update(mud: str, username: str, visname: str, gender: int) -> Packet:
    return Packet("ucache-update", mud, None, None, None, [username, visname, gender])
