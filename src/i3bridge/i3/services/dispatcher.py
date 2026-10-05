"""Routes inbound I3 packets to the frontend and answers queries about our users.

Everything the specification lets another mud ask of us is handled here: channel traffic,
tells, who/finger/locate/chan-who/chan-user requests, replies to our own queries, errors
addressed to our users, and the user cache. Anything else gets the right ``error`` back.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterable

from ...bridge.ports import ChannelEvent, Frontend, PresenceProvider, TellEvent
from .. import packets as pk
from ..client import I3Client

log = logging.getLogger(__name__)

NOT_IMPLEMENTED = frozenset(
    {
        "auth-mud-req",
        "auth-mud-reply",
        "oob-req",
        "oob-begin",
        "oob-end",
        "mail",
        "mail-ack",
        "file-list-req",
        "file-list-reply",
        "file-put",
        "file-put-ack",
        "file-get-req",
        "file-get-reply",
        "news-read-req",
        "news-post-req",
        "news-grplist-req",
        "channel-filter-req",
        "channel-filter-reply",
        "channel-add",
        "channel-remove",
        "channel-admin",
        "channel-user-req",
        "channel-user-reply",
    }
)


UCACHE_MAX = 5000


class Dispatcher:
    """Implements :class:`i3bridge.i3.client.PacketHandler`."""

    def __init__(
        self,
        frontend: Frontend,
        presence: PresenceProvider,
        *,
        ignore_patterns: Callable[[], Iterable[str]] = lambda: (),
        listened: Callable[[], set[str]] | None = None,
    ) -> None:
        self.frontend = frontend
        self.presence = presence
        self._ignore_patterns = ignore_patterns
        self._listened = listened
        self.ucache: OrderedDict[tuple[str, str], tuple[str, int]] = OrderedDict()
        self.counts: dict[str, int] = {}

    def visname_for(self, mud: str, user: str, fallback: str) -> str:
        key = (mud.lower(), user.lower())
        entry = self.ucache.get(key)
        if entry is None:
            return fallback
        self.ucache.move_to_end(key)
        return entry[0] or fallback

    def _ignored(self, mud: str, user: str | None) -> bool:
        from ...bridge.policy import source_matches

        return source_matches(self._ignore_patterns(), mud, user)

    async def handle(self, client: I3Client, pkt: pk.Packet) -> None:
        self.counts[pkt.type] = self.counts.get(pkt.type, 0) + 1
        t = pkt.type
        if t in ("channel-m", "channel-e"):
            await self._channel(pkt)
        elif t == "channel-t":
            await self._channel_t(pkt)
        elif t in ("tell", "emoteto"):
            await self._tell(client, pkt)
        elif t == "who-req":
            await self._who_req(client, pkt)
        elif t == "finger-req":
            await self._finger_req(client, pkt)
        elif t == "locate-req":
            await self._locate_req(client, pkt)
        elif t == "chan-who-req":
            await self._chan_who_req(client, pkt)
        elif t == "chan-user-req":
            await self._chan_user_req(client, pkt)
        elif t == "who-reply":
            if pkt.target_user:
                await self.frontend.on_who_reply(
                    pkt.target_user, pkt.orig_mud, pk.parse_who_reply(pkt)
                )
        elif t == "finger-reply":
            if pkt.target_user:
                await self.frontend.on_finger_reply(
                    pkt.target_user, pkt.orig_mud, pk.parse_finger_reply(pkt)
                )
        elif t == "locate-reply":
            if pkt.target_user:
                await self.frontend.on_locate_reply(
                    pkt.target_user, pkt.orig_mud, pk.parse_locate_reply(pkt)
                )
        elif t == "chan-who-reply":
            if pkt.target_user:
                await self.frontend.on_chan_who_reply(
                    pkt.target_user, pkt.orig_mud, pk.parse_chan_who_reply(pkt)
                )
        elif t == "chan-user-reply":
            pass  # we do not ask for these
        elif t == "error":
            err = pk.parse_error(pkt)
            if pkt.target_user:
                await self.frontend.on_error(pkt.target_user, pkt.orig_mud, err)
            else:
                log.warning("error from %s: %s: %s", pkt.orig_mud, err.code, err.message)
        elif t == "ucache-update":
            u = pk.parse_ucache_update(pkt)
            key = (pkt.orig_mud.lower(), u.username.lower()[:64])
            self.ucache[key] = (u.visname[:80], u.gender)
            self.ucache.move_to_end(key)
            while len(self.ucache) > UCACHE_MAX:
                self.ucache.popitem(last=False)
        elif t in NOT_IMPLEMENTED:
            await self._reply_error(client, pkt, "not-imp", f"{t} is not supported by this mud")
        else:
            await self._reply_error(client, pkt, "unk-type", f"unknown packet type {t}")

    # --------------------------------------------------------------------- channels

    async def _channel(self, pkt: pk.Packet) -> None:
        msg = pk.parse_channel_message(pkt)
        if self._ignored(pkt.orig_mud, pkt.orig_user):
            return
        await self.frontend.on_channel(
            ChannelEvent(
                channel=msg.channel,
                mud=pkt.orig_mud,
                user=pkt.orig_user or "",
                visname=msg.visname
                or self.visname_for(pkt.orig_mud, pkt.orig_user or "", pkt.orig_user or ""),
                text=msg.message,
                kind="message" if pkt.type == "channel-m" else "emote",
            )
        )

    async def _channel_t(self, pkt: pk.Packet) -> None:
        t = pk.parse_channel_t(pkt)
        if self._ignored(pkt.orig_mud, pkt.orig_user):
            return
        actor = f"{t.orig_visname}@{pkt.orig_mud}"
        target = f"{t.target_visname}@{t.target_mud}" if t.target_mud else t.target_visname
        rendered = t.message_others.replace("$N", actor).replace("$O", target)
        await self.frontend.on_channel(
            ChannelEvent(
                channel=t.channel,
                mud=pkt.orig_mud,
                user=pkt.orig_user or "",
                visname=t.orig_visname,
                text=rendered,
                kind="targeted",
            )
        )

    # ------------------------------------------------------------------------ tells

    async def _tell(self, client: I3Client, pkt: pk.Packet) -> None:
        tell = pk.parse_tell(pkt)
        if not pkt.target_user:
            await self._reply_error(client, pkt, "unk-user", "no target user given")
            return
        if self._ignored(pkt.orig_mud, pkt.orig_user):
            return  # silently; an ignored sender learns nothing
        delivered = await self.frontend.on_tell(
            TellEvent(
                from_mud=pkt.orig_mud,
                from_user=pkt.orig_user or "",
                from_visname=tell.visname or pkt.orig_user or "",
                to_user=pkt.target_user,
                text=tell.message,
                emote=pkt.type == "emoteto",
            )
        )
        if not delivered:
            await self._reply_error(
                client, pkt, "unk-user", f"No user named {pkt.target_user} on {client.mud_name}"
            )

    # ---------------------------------------------------------------------- queries

    async def _who_req(self, client: I3Client, pkt: pk.Packet) -> None:
        entries = self.presence.who_list()
        await client.send(pk.build_who_reply(client.mud_name, pkt.orig_mud, pkt.orig_user, entries))

    async def _finger_req(self, client: I3Client, pkt: pk.Packet) -> None:
        pk._need(pkt.payload, 1, "finger-req")
        username = pk._str(pkt.payload[0], "username")
        reply = self.presence.finger(username)
        if reply is None:
            await self._reply_error(client, pkt, "unk-user", f"No user named {username}")
            return
        await client.send(
            pk.build_finger_reply(client.mud_name, pkt.orig_mud, pkt.orig_user, reply)
        )

    async def _locate_req(self, client: I3Client, pkt: pk.Packet) -> None:
        pk._need(pkt.payload, 1, "locate-req")
        username = pk._str(pkt.payload[0], "username")
        info = self.presence.locate(username)
        if info is None:
            return  # only muds that have the user answer
        await client.send(
            pk.build_locate_reply(
                client.mud_name,
                pkt.orig_mud,
                pkt.orig_user,
                client.mud_name,
                info.visname,
                info.idle,
                info.status,
            )
        )

    async def _chan_who_req(self, client: I3Client, pkt: pk.Packet) -> None:
        pk._need(pkt.payload, 1, "chan-who-req")
        channel = pk._str(pkt.payload[0], "channel")
        users = self.presence.chan_who(channel)
        if users is None:
            await self._reply_error(
                client, pkt, "unk-channel", f"{client.mud_name} does not carry {channel}"
            )
            return
        await client.send(
            pk.build_chan_who_reply(client.mud_name, pkt.orig_mud, pkt.orig_user, channel, users)
        )

    async def _chan_user_req(self, client: I3Client, pkt: pk.Packet) -> None:
        pk._need(pkt.payload, 1, "chan-user-req")
        username = pk._str(pkt.payload[0], "username")
        found = self.presence.chan_user(username)
        if found is None:
            await self._reply_error(client, pkt, "unk-user", f"No user named {username}")
            return
        visname, gender = found
        await client.send(
            pk.build_chan_user_reply(client.mud_name, pkt.orig_mud, username, visname, gender)
        )

    # ----------------------------------------------------------------------- errors

    async def _reply_error(self, client: I3Client, pkt: pk.Packet, code: str, message: str) -> None:
        if not pkt.orig_mud or pkt.orig_mud.startswith("*"):
            return  # never bounce errors to a router
        await client.send(
            pk.build_error(
                client.mud_name, pkt.orig_mud, pkt.orig_user, code, message, pkt.to_lpc()
            )
        )
