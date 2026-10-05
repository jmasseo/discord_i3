"""The ``/i3`` slash-command group."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from ..bridge.identity import IdentityError
from ..i3.services import BridgeOffline

if TYPE_CHECKING:
    from ..bridge.core import Bridge

EPHEMERAL_MAX = 1900


def _clip(text: str) -> str:
    return text if len(text) <= EPHEMERAL_MAX else text[: EPHEMERAL_MAX - 20] + "\n… (truncated)"


def build_group(bridge: Bridge) -> app_commands.Group:
    cfg = bridge.cfg.discord
    group = app_commands.Group(
        name="i3",
        description="InterMUD-3 bridge",
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=False
        ),
        allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
    )

    def is_admin(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not isinstance(user, discord.Member):
            return False
        if bridge.admin_role_id and any(r.id == bridge.admin_role_id for r in user.roles):
            return True
        return bool(user.guild_permissions.manage_guild)

    async def deny(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "That command needs the bridge admin role.", ephemeral=True
        )

    async def reply(interaction: discord.Interaction, text: str) -> None:
        text = _clip(text or "(nothing)")
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------------ browsing

    @group.command(name="channels", description="List I3 channels on the network")
    @app_commands.describe(filter="only show channels containing this text")
    async def channels(interaction: discord.Interaction, filter: str | None = None) -> None:
        await reply(interaction, bridge.format_chanlist(filter))

    @group.command(name="mudlist", description="List muds on the I3 network")
    @app_commands.describe(filter="only show muds containing this text")
    async def mudlist(interaction: discord.Interaction, filter: str | None = None) -> None:
        await reply(interaction, bridge.format_mudlist(filter))

    @group.command(name="status", description="Bridge status (admin)")
    async def status(interaction: discord.Interaction) -> None:
        if not is_admin(interaction):
            return await deny(interaction)
        await reply(interaction, bridge.status_text())

    # ------------------------------------------------------- channel links

    @group.command(name="join", description="Mirror an I3 channel into a Discord channel")
    @app_commands.describe(channel="I3 channel name (see /i3 channels)")
    async def join(interaction: discord.Interaction, channel: str) -> None:
        if interaction.guild is None:
            return await reply(interaction, "Run this in the server.")
        if not (is_admin(interaction) or cfg.allow_member_join):
            return await deny(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            link = await bridge.join_channel(channel)
        except (ValueError, BridgeOffline, discord.HTTPException) as exc:
            return await reply(interaction, f"Could not join: {exc}")
        await reply(
            interaction, f"Now relaying I3 **{link.i3_channel}** in <#{link.discord_channel_id}>."
        )

    @group.command(name="leave", description="Stop mirroring an I3 channel")
    @app_commands.describe(
        channel="I3 channel name", delete="also delete the Discord channel the bridge created"
    )
    async def leave(interaction: discord.Interaction, channel: str, delete: bool = False) -> None:
        if not is_admin(interaction):
            return await deny(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            await reply(interaction, await bridge.leave_channel(channel, delete=delete))
        except (ValueError, discord.HTTPException) as exc:
            await reply(interaction, f"Could not leave: {exc}")

    @group.command(name="link", description="Mirror an I3 channel into an existing Discord channel")
    @app_commands.describe(channel="I3 channel name", target="the Discord channel to use")
    async def link(
        interaction: discord.Interaction, channel: str, target: discord.TextChannel
    ) -> None:
        if not is_admin(interaction):
            return await deny(interaction)
        await interaction.response.defer(ephemeral=True)
        try:
            lnk = await bridge.link_channel(channel, target)
        except (ValueError, BridgeOffline, discord.HTTPException) as exc:
            return await reply(interaction, f"Could not link: {exc}")
        await reply(
            interaction, f"Now relaying I3 **{lnk.i3_channel}** in <#{lnk.discord_channel_id}>."
        )

    # ------------------------------------------------------------- queries

    @group.command(name="who", description="Who is on a mud (rwho)")
    @app_commands.describe(mud="mud name")
    async def who(interaction: discord.Interaction, mud: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(interaction, await bridge.query_who(interaction.user, mud))

    @group.command(name="finger", description="Finger a user on another mud")
    @app_commands.describe(target="user@mud")
    async def finger(interaction: discord.Interaction, target: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(interaction, await bridge.query_finger(interaction.user, target))

    @group.command(name="locate", description="Find which mud a user is on")
    @app_commands.describe(user="user name")
    async def locate(interaction: discord.Interaction, user: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(interaction, await bridge.query_locate(interaction.user, user))

    @group.command(name="chanwho", description="Who on a mud listens to a channel")
    @app_commands.describe(mud="mud name", channel="I3 channel name")
    async def chanwho(interaction: discord.Interaction, mud: str, channel: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(interaction, await bridge.query_chanwho(interaction.user, mud, channel))

    # -------------------------------------------------------------- talking

    @group.command(name="tell", description="Send a tell to someone on another mud")
    @app_commands.describe(target="user@mud", message="what to say")
    async def tell(interaction: discord.Interaction, target: str, message: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(interaction, await bridge.send_tell(interaction.user, target, message))

    @group.command(name="emoteto", description="Send an emote to someone on another mud")
    @app_commands.describe(target="user@mud", message="the emote, e.g. 'waves'")
    async def emoteto(interaction: discord.Interaction, target: str, message: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await reply(
            interaction, await bridge.send_tell(interaction.user, target, message, emote=True)
        )

    @group.command(
        name="me", description="Emote on the I3 channel mirrored by this Discord channel"
    )
    @app_commands.describe(text="the emote, e.g. 'waves'")
    async def me(interaction: discord.Interaction, text: str) -> None:
        if interaction.guild is None or interaction.channel is None:
            return await reply(interaction, "Run this in a mirrored channel.")
        await interaction.response.defer(ephemeral=True)
        await reply(
            interaction, await bridge.send_emote(interaction.user, interaction.channel.id, text)
        )

    # ------------------------------------------------------------- identity

    @group.command(name="nick", description="Set your I3 user name")
    @app_commands.describe(name="2-20 letters/digits, lowercase")
    async def nick(interaction: discord.Interaction, name: str) -> None:
        try:
            rec = bridge.identity.set_nick(interaction.user.id, interaction.user.display_name, name)
        except IdentityError as exc:
            return await reply(interaction, str(exc))
        await reply(interaction, f"You are now **{rec.i3_name}@{bridge.mud_name}** on I3.")

    @group.command(name="gender", description="Set the gender other muds see for you")
    async def gender(
        interaction: discord.Interaction, gender: Literal["male", "female", "neuter"]
    ) -> None:
        bridge.identity.set_gender(interaction.user.id, interaction.user.display_name, gender)
        await reply(interaction, f"Gender set to {gender}.")

    @group.command(name="hide", description="Hide yourself from rwho/finger/locate on I3")
    async def hide(interaction: discord.Interaction) -> None:
        bridge.identity.set_hidden(interaction.user.id, interaction.user.display_name, True)
        await reply(interaction, "You are hidden from I3 who lists.")

    @group.command(name="show", description="Show yourself on rwho/finger/locate again")
    async def show(interaction: discord.Interaction) -> None:
        bridge.identity.set_hidden(interaction.user.id, interaction.user.display_name, False)
        await reply(interaction, "You are visible on I3 who lists.")

    @group.command(
        name="dm", description="Receive I3 tells as DMs (on) or only in the tells channel (off)"
    )
    async def dm(interaction: discord.Interaction, mode: Literal["on", "off"]) -> None:
        bridge.identity.set_dm(interaction.user.id, interaction.user.display_name, mode == "on")
        await reply(interaction, f"Tells by DM: {mode}.")

    @group.command(name="whoami", description="Show your I3 identity")
    async def whoami(interaction: discord.Interaction) -> None:
        rec = bridge.identity.ensure(interaction.user.id, interaction.user.display_name)
        await reply(
            interaction,
            f"You are **{rec.i3_name}@{bridge.mud_name}** (shown as {rec.visname}); "
            f"hidden: {rec.hidden}; tells by DM: {rec.dm_tells}.",
        )

    # --------------------------------------------------------------- admin

    @group.command(name="ignore", description="Manage ignored I3 sources (admin)")
    @app_commands.describe(pattern="user@mud, @mud, or a wildcard like troll*@*")
    async def ignore(
        interaction: discord.Interaction,
        action: Literal["add", "remove", "list"],
        pattern: str | None = None,
    ) -> None:
        if not is_admin(interaction):
            return await deny(interaction)
        store = bridge.store
        if action == "list":
            return await reply(
                interaction, "Ignored: " + (", ".join(store.list_ignores()) or "nothing")
            )
        if not pattern:
            return await reply(interaction, "Give a pattern.")
        if action == "add":
            store.add_ignore(pattern)
            return await reply(interaction, f"Ignoring {pattern.lower()}.")
        removed = store.remove_ignore(pattern)
        await reply(interaction, f"Removed {pattern.lower()}." if removed else "No such pattern.")

    return group
