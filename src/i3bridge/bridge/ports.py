"""Interfaces between the I3 service layer and a frontend (Discord, or the console REPL).

The service dispatcher never imports Discord. It talks to a :class:`Frontend` for
delivering inbound traffic and to a :class:`PresenceProvider` for answering queries about
our users. Both are small enough for a test double.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ..i3.packets import (
    ChanWhoReply,
    ErrorPacket,
    FingerReply,
    LocateReply,
    WhoEntry,
)


@dataclass(slots=True, frozen=True)
class ChannelEvent:
    channel: str
    mud: str
    user: str
    visname: str
    text: str
    kind: Literal["message", "emote", "targeted"]


@dataclass(slots=True, frozen=True)
class TellEvent:
    from_mud: str
    from_user: str
    from_visname: str
    to_user: str  # our user's I3 name
    text: str
    emote: bool


@dataclass(slots=True, frozen=True)
class LocateInfo:
    visname: str
    idle: int
    status: str | None  # spec vocabulary: None (active), "inactive", "link-dead", ...


class Frontend(Protocol):
    async def on_channel(self, event: ChannelEvent) -> None: ...

    async def on_tell(self, event: TellEvent) -> bool:
        """Deliver a tell to one of our users. Return False if the user is unknown."""
        ...

    async def on_who_reply(self, to_user: str, from_mud: str, entries: list[WhoEntry]) -> None: ...

    async def on_finger_reply(self, to_user: str, from_mud: str, reply: FingerReply) -> None: ...

    async def on_locate_reply(self, to_user: str, from_mud: str, reply: LocateReply) -> None: ...

    async def on_chan_who_reply(self, to_user: str, from_mud: str, reply: ChanWhoReply) -> None: ...

    async def on_error(self, to_user: str, from_mud: str, error: ErrorPacket) -> None: ...


class PresenceProvider(Protocol):
    def who_list(self) -> list[WhoEntry]: ...

    def finger(self, username: str) -> FingerReply | None: ...

    def locate(self, username: str) -> LocateInfo | None: ...

    def chan_who(self, channel: str) -> list[str] | None:
        """Visnames of our users on ``channel``; None if we do not carry the channel."""
        ...

    def chan_user(self, username: str) -> tuple[str, int] | None:
        """``(visname, gender)`` for one of our users, or None."""
        ...
