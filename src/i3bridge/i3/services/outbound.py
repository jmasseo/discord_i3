"""What a frontend may ask the I3 side to do. Thin, typed wrappers over ``client.send``."""

from __future__ import annotations

from .. import packets as pk
from ..client import I3Client, NotConnectedError


class BridgeOffline(RuntimeError):
    """Raised to frontends when the router link is down; show it to the user."""


class Outbound:
    def __init__(self, client: I3Client) -> None:
        self.client = client

    @property
    def mud(self) -> str:
        return self.client.mud_name

    async def _send(self, pkt: pk.Packet) -> None:
        try:
            await self.client.send(pkt)
        except (NotConnectedError, OSError) as exc:
            raise BridgeOffline("not connected to the I3 network right now") from exc

    async def listen(self, channel: str, on: bool) -> None:
        try:
            await self.client.listen(channel, on)
        except (NotConnectedError, OSError) as exc:
            raise BridgeOffline("not connected to the I3 network right now") from exc

    async def channel_message(self, channel: str, user: str, visname: str, text: str) -> None:
        await self._send(pk.build_channel_m(self.mud, user, channel, visname, text))

    async def channel_emote(self, channel: str, user: str, visname: str, text: str) -> None:
        await self._send(pk.build_channel_e(self.mud, user, channel, visname, text))

    async def tell(
        self, user: str, visname: str, target_mud: str, target_user: str, text: str
    ) -> None:
        await self._send(pk.build_tell(self.mud, user, target_mud, target_user, visname, text))

    async def emoteto(
        self, user: str, visname: str, target_mud: str, target_user: str, text: str
    ) -> None:
        await self._send(pk.build_emoteto(self.mud, user, target_mud, target_user, visname, text))

    async def who(self, user: str, target_mud: str) -> None:
        await self._send(pk.build_who_req(self.mud, user, target_mud))

    async def finger(self, user: str, target_mud: str, username: str) -> None:
        await self._send(pk.build_finger_req(self.mud, user, target_mud, username))

    async def locate(self, user: str, username: str) -> None:
        await self._send(pk.build_locate_req(self.mud, user, username))

    async def chan_who(self, user: str, target_mud: str, channel: str) -> None:
        await self._send(pk.build_chan_who_req(self.mud, user, target_mud, channel))

    async def ucache(self, user: str, visname: str, gender: int) -> None:
        await self._send(pk.build_ucache_update(self.mud, user, visname, gender))

    async def channel_add(self, user: str, channel: str, channel_type: int) -> None:
        await self._send(
            pk.build_channel_add(self.mud, user, self.client.router_name, channel, channel_type)
        )

    async def channel_remove(self, user: str, channel: str) -> None:
        await self._send(pk.build_channel_remove(self.mud, user, self.client.router_name, channel))

    async def channel_admin(
        self, user: str, channel: str, add_muds: list[str], remove_muds: list[str]
    ) -> None:
        await self._send(
            pk.build_channel_admin(
                self.mud, user, self.client.router_name, channel, add_muds, remove_muds
            )
        )
