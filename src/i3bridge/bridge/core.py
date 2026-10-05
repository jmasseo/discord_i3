"""The bridge: wires Discord to the I3 layer and implements the Frontend contract."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord

from ..config import Config
from ..discord_.bot import BridgeBot, snapshot_of
from ..discord_.channels import ChannelManager
from ..discord_.format import (
    ascii_name,
    clean_inbound,
    discord_to_i3,
    italic_emote,
    render_channel_event,
    webhook_username,
)
from ..discord_.relay import NO_MENTIONS, Relay
from ..i3 import packets as pk
from ..i3.client import I3Client
from ..i3.services import BridgeOffline, Dispatcher, Outbound
from ..state.db import ChannelLink, StateStore, UserRecord
from .identity import IdentityMap
from .policy import FloodBreaker, RateLimiter
from .ports import ChannelEvent, TellEvent
from .presence import PresenceTable
from .tells import TellRouter

log = logging.getLogger(__name__)

QUERY_TIMEOUT = 10.0
LOCATE_COLLECT = 3.0


@dataclass(slots=True)
class _Pending:
    kind: str
    future: asyncio.Future[str]
    collected: list[str] = field(default_factory=list)


def human_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_who(mud: str, entries: list[pk.WhoEntry]) -> str:
    if not entries:
        return f"**{mud}**: nobody is on."
    rows = [
        f"{clean_inbound(e.visname)[:20]:<20} {human_idle(e.idle):>5}  {clean_inbound(e.extra)}"
        for e in entries
    ]
    return f"**{mud}**: {len(entries)} on\n```\n" + "\n".join(rows)[:1700] + "\n```"


class Bridge:
    """Owns every component; implements :class:`i3bridge.bridge.ports.Frontend`."""

    def __init__(self, cfg: Config, store: StateStore, bot: BridgeBot) -> None:
        self.cfg = cfg
        self.store = store
        self.bot = bot
        self.mud_name = cfg.mud.name
        self.identity = IdentityMap(store)
        self.presence = PresenceTable(
            self.identity,
            cfg.presence,
            can_view=self.can_view,
            bridged_channels=self.bridged_channels,
        )
        self.dispatcher = Dispatcher(self, self.presence, ignore_patterns=store.list_ignores)
        self.client = I3Client(cfg, store, self.dispatcher)
        self.outbound = Outbound(self.client)
        self.channels = ChannelManager(bot, cfg.discord, store)
        self.relay = Relay(bot, recreate_webhook=self.channels.recreate_webhook)
        self.flood = FloodBreaker(
            cfg.limits.channel_flood_messages, cfg.limits.channel_flood_window_seconds
        )
        self.user_limiter = RateLimiter(cfg.limits.messages_per_user_per_minute, 60.0)
        self.admin_role_id = cfg.discord.admin_role_id
        self.tells_channel_id = cfg.discord.tells_channel_id
        self.tells = TellRouter(
            self.identity,
            store,
            self.outbound,
            limiter=RateLimiter(cfg.limits.tells_per_minute, 60.0),
            send_dm=self.send_dm,
            send_fallback=self.send_fallback,
            transliterate=cfg.format.transliterate,
            max_chars=cfg.format.max_outbound_chars,
        )
        self._pending: dict[str, list[_Pending]] = {}
        self._discord_ready = False
        self.started = time.time()
        for link in store.get_links():
            self.client.stats.listened_channels.add(link.i3_channel)
        self.client.on_chanlist.append(self._on_chanlist)
        self.client.on_router_error.append(self._on_router_error)
        self.client.on_ready.append(self._on_i3_ready)

    # ------------------------------------------------------------- lifecycle

    @property
    def ready(self) -> bool:
        return self._discord_ready and self.client.ready

    async def on_discord_ready(self) -> None:
        guild = self.bot.get_guild(self.cfg.discord.guild_id)
        if guild is None:
            log.error("bot is not a member of guild %d; invite it first", self.cfg.discord.guild_id)
            return
        for member in guild.members:
            self.presence.update(snapshot_of(member))
        log.info(
            "presence table primed with %d members (%d online)",
            len(guild.members),
            self.presence.count_online(),
        )
        try:
            await self.channels.ensure_category()
            await self._provision()
            await self._reconcile()
        except discord.HTTPException:
            log.exception("Discord provisioning or reconciliation failed")
        self._discord_ready = True

    async def _provision(self) -> None:
        """Find or create the admin role and the tells channel unless ids were configured."""
        d = self.cfg.discord
        if not d.auto_provision:
            return
        guild = self.channels.guild
        if not self.admin_role_id:
            saved = self.store.get_setting("admin_role_id")
            role = guild.get_role(int(saved)) if saved else None
            if role is None:
                role = await self.channels.ensure_role(d.admin_role_name)
                self.store.set_setting("admin_role_id", str(role.id))
            self.admin_role_id = role.id
            log.info("admin role: @%s (%d)", role.name, role.id)
        if not self.tells_channel_id:
            saved = self.store.get_setting("tells_channel_id")
            channel = self.channels.text_channel(int(saved)) if saved else None
            if channel is None:
                channel = await self.channels.ensure_text_channel(
                    d.tells_channel_name,
                    "I3 tells for members whose DMs are closed, and bridge notices. "
                    "Reply to tells by DMing the bot.",
                )
                self.store.set_setting("tells_channel_id", str(channel.id))
            self.tells_channel_id = channel.id
            log.info("tells channel: #%s (%d)", channel.name, channel.id)

    async def _on_i3_ready(self) -> None:
        log.info(
            "I3 ready on %s; relaying %d channels",
            self.client.router_name,
            len(self.store.get_links()),
        )

    async def _on_chanlist(self) -> None:
        if self._discord_ready:
            await self._reconcile()

    async def _reconcile(self) -> None:
        notes = await self.channels.reconcile(self.store.get_chanlist())
        for channel_id, note in notes:
            log.warning("%s", note)
            if channel_id is not None:
                await self.relay.notice(channel_id, note)
            else:
                await self._admin_notice(note)
        linked = self.bridged_channels()
        for stale in sorted(self.client.stats.listened_channels - linked):
            log.info("no longer linked to %s; leaving the I3 channel", stale)
            with contextlib.suppress(BridgeOffline):
                await self.outbound.listen(stale, False)
            self.client.stats.listened_channels.discard(stale)
        self.client.stats.listened_channels.update(linked)

    async def _on_router_error(self, err: pk.ErrorPacket) -> None:
        await self._admin_notice(f"Router error `{err.code}`: {clean_inbound(err.message)}")

    async def _admin_notice(self, text: str) -> None:
        cid = self.tells_channel_id
        if cid:
            await self.relay.notice(cid, text)

    async def on_channel_deleted(self, channel_id: int) -> None:
        link = self.store.link_for_discord(channel_id)
        if link is None:
            return
        log.info("Discord channel for %s deleted; leaving the I3 channel", link.i3_channel)
        self.store.remove_link(link.i3_channel)
        with contextlib.suppress(BridgeOffline):
            await self.outbound.listen(link.i3_channel, False)

    async def on_webhooks_changed(self, channel_id: int) -> None:
        link = self.store.link_for_discord(channel_id)
        if link is None or link.webhook_id is None:
            return
        channel = self.channels.text_channel(channel_id)
        if channel is None:
            return
        try:
            hooks = await channel.webhooks()
        except discord.HTTPException:
            return
        if all(h.id != link.webhook_id for h in hooks):
            await self.channels.recreate_webhook(link)

    # ---------------------------------------------------------- Frontend API

    async def on_channel(self, ev: ChannelEvent) -> None:
        link = self.store.link_for_i3(ev.channel)
        if link is None:
            return
        action = self.flood.record(ev.channel)
        if action == "trip":
            await self.relay.notice(
                link.discord_channel_id,
                f"{ev.channel} is flooding; pausing relay for about "
                f"{self.cfg.limits.channel_flood_window_seconds}s.",
            )
            return
        if action == "drop":
            return
        dropped = self.flood.take_dropped(ev.channel)
        if dropped:
            await self.relay.notice(
                link.discord_channel_id, f"Relay resumed; {dropped} messages were skipped."
            )
        self.relay.post(
            link,
            webhook_username(ev.visname, ev.mud),
            render_channel_event(ev, self.cfg.format.color_mode),
        )

    async def on_tell(self, ev: TellEvent) -> bool:
        return await self.tells.inbound(ev)

    async def on_who_reply(self, to_user: str, from_mud: str, entries: list[pk.WhoEntry]) -> None:
        await self._deliver(to_user, "who", format_who(from_mud, entries))

    async def on_finger_reply(self, to_user: str, from_mud: str, r: pk.FingerReply) -> None:
        parts = [f"**{clean_inbound(r.visname)}@{from_mud}** {clean_inbound(r.title)}"]
        if r.loginout_time:
            parts.append(f"login/out: {clean_inbound(r.loginout_time)}")
        parts.append(f"idle: {human_idle(r.idle)}")
        if r.level:
            parts.append(f"level: {clean_inbound(r.level)}")
        if r.extra:
            parts.append(clean_inbound(r.extra))
        await self._deliver(to_user, "finger", "\n".join(parts))

    async def on_locate_reply(self, to_user: str, from_mud: str, r: pk.LocateReply) -> None:
        status = f" ({clean_inbound(r.status)})" if r.status else ""
        await self._deliver(
            to_user,
            "locate",
            f"**{clean_inbound(r.located_visname)}** is on **{r.located_mud}**, "
            f"idle {human_idle(r.idle)}{status}",
        )

    async def on_chan_who_reply(self, to_user: str, from_mud: str, r: pk.ChanWhoReply) -> None:
        names = ", ".join(clean_inbound(u) for u in r.users) or "nobody"
        await self._deliver(to_user, "chanwho", f"**{r.channel}** on **{from_mud}**: {names}")

    async def on_error(self, to_user: str, from_mud: str, err: pk.ErrorPacket) -> None:
        await self._deliver(to_user, "*", self.tells.format_error(from_mud, err))

    # ------------------------------------------------------------- queries

    async def _deliver(self, to_user: str, kind: str, text: str) -> None:
        waiting = self._pending.get(to_user.lower(), [])
        for p in waiting:
            if kind == "*" or p.kind == kind:
                if p.kind == "locate" and kind != "*":
                    p.collected.append(text)
                elif not p.future.done():
                    p.future.set_result(text)
                return
        record = self.identity.by_i3_name(to_user)
        if record is not None:
            await self.send_dm(record.discord_id, text)

    async def _query(
        self,
        record: UserRecord,
        kind: str,
        send: Callable[[], Awaitable[None]],
        *,
        collect: float = 0.0,
    ) -> str:
        key = record.i3_name.lower()
        pending = _Pending(kind, asyncio.get_running_loop().create_future())
        self._pending.setdefault(key, []).append(pending)
        try:
            try:
                await send()
            except BridgeOffline as exc:
                return f"Not sent: {exc}"
            if collect:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending.future), collect)
                if pending.future.done():
                    return pending.future.result()
                return "\n".join(pending.collected) or "No mud reported that user."
            try:
                return await asyncio.wait_for(pending.future, QUERY_TIMEOUT)
            except TimeoutError:
                return f"No reply within {QUERY_TIMEOUT:.0f}s."
        finally:
            self._pending[key].remove(pending)
            if not self._pending[key]:
                del self._pending[key]

    def _user(self, user: discord.User | discord.Member) -> UserRecord:
        return self.identity.ensure(user.id, user.display_name)

    def _mud(self, name: str) -> str:
        found = self.store.find_mud(name.strip())
        if found is None:
            raise ValueError(f"no mud named {name} on the network (try /i3 mudlist)")
        return found[0]

    async def query_who(self, user: discord.User | discord.Member, mud: str) -> str:
        rec = self._user(user)
        try:
            target = self._mud(mud)
        except ValueError as exc:
            return str(exc)
        return await self._query(rec, "who", lambda: self.outbound.who(rec.i3_name, target))

    async def query_finger(self, user: discord.User | discord.Member, spec: str) -> str:
        rec = self._user(user)
        try:
            peer, mud = self.tells.parse_target(spec)
        except ValueError as exc:
            return str(exc)
        return await self._query(
            rec, "finger", lambda: self.outbound.finger(rec.i3_name, mud, peer)
        )

    async def query_locate(self, user: discord.User | discord.Member, username: str) -> str:
        rec = self._user(user)
        name = username.strip().lower()
        if not name:
            return "Give a user name."
        return await self._query(
            rec, "locate", lambda: self.outbound.locate(rec.i3_name, name), collect=LOCATE_COLLECT
        )

    async def query_chanwho(
        self, user: discord.User | discord.Member, mud: str, channel: str
    ) -> str:
        rec = self._user(user)
        try:
            target = self._mud(mud)
        except ValueError as exc:
            return str(exc)
        return await self._query(
            rec, "chanwho", lambda: self.outbound.chan_who(rec.i3_name, target, channel.strip())
        )

    # ------------------------------------------------------------- talking

    async def send_tell(
        self, user: discord.User | discord.Member, target: str, message: str, *, emote: bool = False
    ) -> str:
        try:
            return await self.tells.send(user.id, user.display_name, target, message, emote=emote)
        except ValueError as exc:
            return str(exc)

    async def send_emote(
        self, user: discord.User | discord.Member, channel_id: int, text: str
    ) -> str:
        link = self.store.link_for_discord(channel_id)
        if link is None:
            return "This Discord channel does not mirror an I3 channel."
        rec = self._user(user)
        lines = self._lines(text, None)
        if not lines:
            return "Nothing to send."
        try:
            for line in lines:
                await self.outbound.channel_emote(
                    link.i3_channel, rec.i3_name, ascii_name(rec.visname), line
                )
        except BridgeOffline as exc:
            return f"Not sent: {exc}"
        self.presence.saw_message(user.id)
        return f"[{link.i3_channel}] {ascii_name(rec.visname)}@{self.mud_name} {' / '.join(lines)}"

    def _lines(self, content: str, message: discord.Message | None) -> list[str]:
        guild = message.guild if message is not None else None

        def resolve(kind: str, ident: int) -> str | None:
            if guild is None:
                return None
            if kind == "user":
                member = guild.get_member(ident)
                return member.display_name if member else None
            if kind == "channel":
                channel = guild.get_channel(ident)
                return channel.name if channel else None
            role = guild.get_role(ident)
            return role.name if role else None

        return discord_to_i3(
            content,
            resolve=resolve,
            attachments=[a.url for a in message.attachments] if message is not None else (),
            transliterate=self.cfg.format.transliterate,
            max_chars=self.cfg.format.max_outbound_chars,
            multiline=self.cfg.format.multiline,
        )

    async def on_discord_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if message.author.id == (self.bot.user.id if self.bot.user else 0):
            return
        if message.guild is None:
            if isinstance(message.channel, discord.DMChannel):
                text = await self.tells.from_dm(
                    message.author.id, message.author.display_name, message.content
                )
                if text:
                    with contextlib.suppress(discord.HTTPException):
                        await message.channel.send(text, allowed_mentions=NO_MENTIONS)
            return
        if message.guild.id != self.cfg.discord.guild_id:
            return
        link = self.store.link_for_discord(message.channel.id)
        if link is None:
            return
        rec = self._user(message.author)
        self.presence.saw_message(message.author.id)
        self.store.touch_user(message.author.id)
        if not self.user_limiter.allow(message.author.id):
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("⏳")
            return
        emote_text = italic_emote(message.content) if self.cfg.format.emote_from_italics else None
        lines = self._lines(emote_text or message.content, message)
        ref = message.reference.resolved if message.reference else None
        if lines and isinstance(ref, discord.Message) and not emote_text:
            lines[0] = f"[re {ascii_name(ref.author.display_name, max_len=20)}] {lines[0]}"
        if not lines:
            return
        visname = ascii_name(rec.visname)
        try:
            for line in lines:
                if emote_text:
                    await self.outbound.channel_emote(link.i3_channel, rec.i3_name, visname, line)
                else:
                    await self.outbound.channel_message(link.i3_channel, rec.i3_name, visname, line)
        except BridgeOffline:
            with contextlib.suppress(discord.HTTPException):
                await message.add_reaction("⚠️")

    # ------------------------------------------------------- channel links

    def _channel_info(self, name: str) -> tuple[str, pk.ChannelInfo]:
        chanlist = self.store.get_chanlist()
        if name in chanlist:
            return name, chanlist[name]
        for chan, info in chanlist.items():
            if chan.lower() == name.strip().lower():
                return chan, info
        raise ValueError(f"no I3 channel named {name} (see /i3 channels)")

    async def join_channel(self, name: str) -> ChannelLink:
        chan, info = self._channel_info(name)
        link = await self.channels.join(chan, info)
        await self.outbound.listen(chan, True)
        return link

    async def link_channel(self, name: str, target: discord.TextChannel) -> ChannelLink:
        chan, info = self._channel_info(name)
        link = await self.channels.link_existing(chan, target, info)
        await self.outbound.listen(chan, True)
        return link

    async def leave_channel(self, name: str, *, delete: bool = False) -> str:
        chan = name.strip()
        link = self.store.link_for_i3(chan)
        if link is None:
            for lnk in self.store.get_links():
                if lnk.i3_channel.lower() == chan.lower():
                    chan = lnk.i3_channel
                    break
        with contextlib.suppress(BridgeOffline):
            await self.outbound.listen(chan, False)
        return await self.channels.unlink(chan, delete=delete)

    def bridged_channels(self) -> set[str]:
        return {link.i3_channel for link in self.store.get_links()}

    def can_view(self, member_id: int, channel: str) -> bool:
        link = self.store.link_for_i3(channel)
        if link is None:
            return False
        guild = self.bot.get_guild(self.cfg.discord.guild_id)
        if guild is None:
            return False
        member = guild.get_member(member_id)
        text_channel = guild.get_channel(link.discord_channel_id)
        if member is None or not isinstance(text_channel, discord.TextChannel):
            return False
        return bool(text_channel.permissions_for(member).view_channel)

    # ------------------------------------------------------------- messaging

    async def send_dm(self, discord_id: int, text: str) -> bool:
        user = self.bot.get_user(discord_id)
        try:
            if user is None:
                user = await self.bot.fetch_user(discord_id)
            await user.send(text[:1950], allowed_mentions=NO_MENTIONS)
            return True
        except discord.HTTPException:
            return False

    async def send_fallback(self, discord_id: int, text: str) -> bool:
        if not self.tells_channel_id:
            return False
        channel = self.bot.get_channel(self.tells_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return False
        try:
            await channel.send(
                f"<@{discord_id}> {text}"[:1950],
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=discord_id)]),
            )
            return True
        except discord.HTTPException:
            return False

    # ------------------------------------------------------------- reports

    def format_chanlist(self, needle: str | None) -> str:
        chans = self.store.get_chanlist()
        links = {lnk.i3_channel: lnk for lnk in self.store.get_links()}
        rows = []
        for name, info in chans.items():
            if needle and needle.lower() not in name.lower():
                continue
            mark = f" → <#{links[name].discord_channel_id}>" if name in links else ""
            rows.append(f"• **{clean_inbound(name)}** ({clean_inbound(info.host_mud)}){mark}")
        return (f"{len(rows)} channels:\n" + "\n".join(rows)) if rows else "No channels match."

    def format_mudlist(self, needle: str | None) -> str:
        muds = self.store.get_mudlist()
        rows = []
        for name, info in muds.items():
            if needle and needle.lower() not in name.lower():
                continue
            if not info.up and needle is None:
                continue
            state = "up" if info.up else "down"
            rows.append(
                f"• **{clean_inbound(name)}** ({state}) "
                f"{clean_inbound(info.driver)} {clean_inbound(info.mud_type)}"
            )
        up = sum(1 for m in muds.values() if m.up)
        return f"{up} muds up, {len(muds)} known. " + ("\n" + "\n".join(rows) if rows else "")

    def status_text(self) -> str:
        s = self.client.stats
        links = self.store.get_links()
        return (
            f"I3: {s.state.value} on {s.router or '-'}; in {s.packets_in} out {s.packets_out}; "
            f"reconnects {s.reconnects}; last error: {s.last_error or 'none'}\n"
            f"Relaying {len(links)} channels: "
            f"{', '.join(lnk.i3_channel for lnk in links) or 'none'}\n"
            f"Relay posted {self.relay.posted}, failed {self.relay.failed}, "
            f"dropped {self.relay.dropped}, queued {self.relay.queue_depth()}\n"
            f"Admin role id {self.admin_role_id or '-'}; "
            f"tells channel id {self.tells_channel_id or '-'}\n"
            f"Online members listed for rwho: {self.presence.count_online()}; "
            f"uptime {human_idle(int(time.time() - self.started))}"
        )

    def metrics(self) -> dict[str, float]:
        s = self.client.stats
        return {
            "i3_connected": 1.0 if self.client.ready else 0.0,
            "i3_packets_in_total": float(s.packets_in),
            "i3_packets_out_total": float(s.packets_out),
            "i3_reconnects_total": float(s.reconnects),
            "discord_ready": 1.0 if self._discord_ready else 0.0,
            "relay_posted_total": float(self.relay.posted),
            "relay_failed_total": float(self.relay.failed),
            "relay_dropped_total": float(self.relay.dropped),
            "relay_queue_depth": float(self.relay.queue_depth()),
            "linked_channels": float(len(self.store.get_links())),
            "online_members": float(self.presence.count_online()),
        }

    def details(self) -> dict[str, object]:
        s = self.client.stats
        return {
            "i3_state": s.state.value,
            "router": s.router,
            "discord_ready": self._discord_ready,
            "admin_role_id": self.admin_role_id,
            "tells_channel_id": self.tells_channel_id,
            "linked_channels": sorted(self.bridged_channels()),
        }
