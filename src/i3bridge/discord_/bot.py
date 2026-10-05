"""The Discord client: intents, slash-command registration, and event forwarding."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from ..bridge.presence import MemberSnapshot
from ..config import Config

if TYPE_CHECKING:
    from ..bridge.core import Bridge

log = logging.getLogger(__name__)


def snapshot_of(member: discord.Member) -> MemberSnapshot:
    activity: str | None = None
    for act in member.activities:
        if isinstance(act, discord.CustomActivity) and act.name:
            activity = act.name
            break
    if activity is None:
        for act in member.activities:
            if isinstance(act, discord.Spotify):
                activity = f"Listening to {act.title}"
            elif isinstance(act, discord.Game):
                activity = f"Playing {act.name}"
            elif isinstance(act, discord.Streaming):
                activity = f"Streaming {act.name}"
            elif isinstance(act, discord.Activity) and act.name:
                verb = {
                    discord.ActivityType.playing: "Playing",
                    discord.ActivityType.listening: "Listening to",
                    discord.ActivityType.watching: "Watching",
                    discord.ActivityType.competing: "Competing in",
                    discord.ActivityType.streaming: "Streaming",
                }.get(act.type, "")
                activity = f"{verb} {act.name}".strip()
            else:
                continue
            break
    top_role = member.top_role.name if not member.top_role.is_default() else None
    return MemberSnapshot(
        id=member.id,
        display_name=member.display_name,
        bot=member.bot,
        status=member.raw_status,
        activity=activity,
        top_role=top_role,
    )


class BridgeBot(discord.Client):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(intents=intents, chunk_guilds_at_startup=True)
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.bridge: Bridge | None = None

    async def setup_hook(self) -> None:
        from .commands import build_group

        assert self.bridge is not None
        self.tree.add_command(build_group(self.bridge))
        guild = discord.Object(id=self.cfg.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        await self.tree.sync()
        log.info("slash commands synced")

    async def on_ready(self) -> None:
        assert self.bridge is not None
        log.info("Discord ready as %s", self.user)
        await self.bridge.on_discord_ready()

    async def on_message(self, message: discord.Message) -> None:
        if self.bridge is not None:
            await self.bridge.on_discord_message(message)

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.bridge is not None and after.guild.id == self.cfg.discord.guild_id:
            self.bridge.presence.update(snapshot_of(after))

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.bridge is not None and after.guild.id == self.cfg.discord.guild_id:
            self.bridge.presence.update(snapshot_of(after))

    async def on_member_join(self, member: discord.Member) -> None:
        if self.bridge is not None and member.guild.id == self.cfg.discord.guild_id:
            self.bridge.presence.update(snapshot_of(member))

    async def on_member_remove(self, member: discord.Member) -> None:
        if self.bridge is not None:
            self.bridge.presence.remove(member.id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self.bridge is not None:
            await self.bridge.on_channel_deleted(channel.id)

    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        if self.bridge is not None:
            await self.bridge.on_webhooks_changed(channel.id)
