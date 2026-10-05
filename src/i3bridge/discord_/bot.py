"""The Discord client: intents, slash-command registration, and event forwarding.

Everything is scoped to the one configured guild: commands are registered there only, the
command tree rejects interactions from anywhere else, event handlers ignore other guilds,
and the bot leaves any other guild it finds itself in.
"""

from __future__ import annotations

import contextlib
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


class GuildOnlyTree(app_commands.CommandTree["BridgeBot"]):
    """Rejects every interaction that does not come from the configured guild."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == self.client.cfg.discord.guild_id:
            return True
        log.warning(
            "rejected %s interaction from guild %s by user %s",
            interaction.command.qualified_name if interaction.command else "unknown",
            interaction.guild_id,
            interaction.user.id,
        )
        if not interaction.response.is_done():
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(
                    "This bridge only takes commands in its own server.", ephemeral=True
                )
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("slash command failed", exc_info=error)
        try:
            text = "Something went wrong running that command."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class BridgeBot(discord.Client):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(intents=intents, chunk_guilds_at_startup=True)
        self.cfg = cfg
        self.tree = GuildOnlyTree(self)
        self.bridge: Bridge | None = None

    @property
    def home_guild(self) -> discord.Object:
        return discord.Object(id=self.cfg.discord.guild_id)

    def _ours(self, guild: discord.Guild | None) -> bool:
        return guild is not None and guild.id == self.cfg.discord.guild_id

    async def setup_hook(self) -> None:
        from .commands import build_group

        assert self.bridge is not None
        self.tree.add_command(build_group(self.bridge), guild=self.home_guild)
        await self.tree.sync(guild=self.home_guild)
        # Earlier versions registered the commands globally; push an empty global set so
        # Discord forgets them and nothing is invocable from other servers or DMs.
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        log.info("slash commands synced to guild %d only", self.cfg.discord.guild_id)

    async def _leave_if_foreign(self, guild: discord.Guild) -> bool:
        if self._ours(guild) or not self.cfg.discord.leave_other_guilds:
            return False
        log.warning("leaving guild %r (%d): not the configured server", guild.name, guild.id)
        try:
            await guild.leave()
        except discord.HTTPException:
            log.exception("could not leave guild %d", guild.id)
        return True

    async def on_ready(self) -> None:
        assert self.bridge is not None
        log.info("Discord ready as %s", self.user)
        for guild in list(self.guilds):
            await self._leave_if_foreign(guild)
        await self.bridge.on_discord_ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._leave_if_foreign(guild)

    async def on_message(self, message: discord.Message) -> None:
        if self.bridge is None:
            return
        if message.guild is not None and not self._ours(message.guild):
            return
        await self.bridge.on_discord_message(message)

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.bridge is not None and self._ours(after.guild):
            self.bridge.presence.update(snapshot_of(after))

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.bridge is not None and self._ours(after.guild):
            self.bridge.presence.update(snapshot_of(after))

    async def on_member_join(self, member: discord.Member) -> None:
        if self.bridge is not None and self._ours(member.guild):
            self.bridge.presence.update(snapshot_of(member))

    async def on_member_remove(self, member: discord.Member) -> None:
        if self.bridge is not None and self._ours(member.guild):
            self.bridge.presence.remove(member.id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self.bridge is not None and self._ours(channel.guild):
            await self.bridge.on_channel_deleted(channel.id)

    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        if self.bridge is not None and self._ours(channel.guild):
            await self.bridge.on_webhooks_changed(channel.id)
