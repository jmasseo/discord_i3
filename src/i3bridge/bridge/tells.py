"""Two-way tells over Discord DMs.

Inbound tells are delivered as DMs (or to a fallback channel). A DM conversation with the
bot is a tell console: plain text replies to the last correspondent, ``user@mud text``
addresses someone explicitly, ``:text`` sends an emoteto to the last correspondent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from ..discord_.format import ascii_name, clean_inbound, discord_to_i3
from ..i3.packets import ErrorPacket
from ..i3.services import BridgeOffline, Outbound
from ..state.db import StateStore
from .identity import IdentityMap
from .policy import RateLimiter
from .ports import TellEvent

log = logging.getLogger(__name__)

Sender = Callable[[int, str], Awaitable[bool]]


class TellRouter:
    def __init__(
        self,
        identity: IdentityMap,
        store: StateStore,
        outbound: Outbound,
        *,
        limiter: RateLimiter,
        send_dm: Sender,
        send_fallback: Sender | None = None,
        transliterate: str = "text",
        max_chars: int = 900,
    ) -> None:
        self.identity = identity
        self.store = store
        self.outbound = outbound
        self.limiter = limiter
        self.send_dm = send_dm
        self.send_fallback = send_fallback
        self.transliterate = transliterate
        self.max_chars = max_chars

    # ------------------------------------------------------------------ inbound

    @staticmethod
    def format_inbound(ev: TellEvent) -> str:
        who = f"**{ev.from_visname}@{ev.from_mud}**"
        text = clean_inbound(ev.text)
        return f"{who} {text}" if ev.emote else f"{who} tells you: {text}"

    async def inbound(self, ev: TellEvent) -> bool:
        user = self.identity.by_i3_name(ev.to_user)
        if user is None:
            return False
        text = self.format_inbound(ev)
        delivered = False
        if user.dm_tells:
            delivered = await self.send_dm(user.discord_id, text)
        if not delivered and self.send_fallback is not None:
            delivered = await self.send_fallback(user.discord_id, text)
        if delivered:
            self.store.set_tell_target(user.discord_id, ev.from_user, ev.from_mud)
        return delivered

    def format_error(self, from_mud: str, err: ErrorPacket) -> str:
        return f"**{from_mud}** reports an error: `{err.code}` {clean_inbound(err.message)}"

    # ----------------------------------------------------------------- outbound

    def parse_target(self, spec: str) -> tuple[str, str]:
        user, _, mud = spec.strip().partition("@")
        if not user or not mud:
            raise ValueError("address people as user@mud")
        found = self.store.find_mud(mud)
        if found is None:
            raise ValueError(f"no mud named {mud} on the network (try /i3 mudlist)")
        return user.lower(), found[0]

    def _lines(self, text: str) -> list[str]:
        return discord_to_i3(
            text,
            resolve=lambda _kind, _id: None,
            transliterate=self.transliterate,
            max_chars=self.max_chars,
            multiline="join",
        )

    async def send(
        self, discord_id: int, display_name: str, target: str, text: str, *, emote: bool = False
    ) -> str:
        """Send a tell/emoteto; return a confirmation line for the user."""
        peer_user, peer_mud = self.parse_target(target)
        record = self.identity.ensure(discord_id, display_name)
        if not self.limiter.allow(discord_id):
            return "You are sending tells too quickly; wait a moment."
        lines = self._lines(text)
        if not lines:
            return "Nothing to send."
        visname = ascii_name(record.visname)
        try:
            for line in lines:
                if emote:
                    await self.outbound.emoteto(record.i3_name, visname, peer_mud, peer_user, line)
                else:
                    await self.outbound.tell(record.i3_name, visname, peer_mud, peer_user, line)
        except BridgeOffline as exc:
            return f"Not sent: {exc}"
        self.store.set_tell_target(discord_id, peer_user, peer_mud)
        shown = " / ".join(lines)
        if emote:
            return f"You emote to {peer_user}@{peer_mud}: {visname} {shown}"
        return f"You tell {peer_user}@{peer_mud}: {shown}"

    async def from_dm(self, discord_id: int, display_name: str, content: str) -> str:
        """Interpret a DM typed to the bot."""
        content = content.strip()
        if not content:
            return ""
        if content.startswith("/"):
            return "Slash commands are not needed here: just type a reply, or `user@mud message`."
        emote = False
        if content.startswith(":"):
            emote = True
            content = content[1:].strip()
        head, _, rest = content.partition(" ")
        if "@" in head and not head.startswith("@") and rest:
            try:
                return await self.send(discord_id, display_name, head, rest, emote=emote)
            except ValueError as exc:
                return str(exc)
        target = self.store.get_tell_target(discord_id)
        if target is None:
            return (
                "Nobody has told you anything yet. Address someone as `user@mud message` "
                "(see /i3 mudlist for mud names)."
            )
        try:
            return await self.send(
                discord_id,
                display_name,
                f"{target.peer_user}@{target.peer_mud}",
                content,
                emote=emote,
            )
        except ValueError as exc:
            return str(exc)
