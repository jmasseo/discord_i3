"""Creates, links, reconciles and removes the Discord channels that mirror I3 channels."""

from __future__ import annotations

import contextlib
import logging
import re

import discord

from ..config import DiscordConfig
from ..i3.packets import ChannelInfo
from ..state.db import ChannelLink, StateStore
from .relay import WEBHOOK_NAME

log = logging.getLogger(__name__)

CHANNEL_TYPES = {0: "selectively banned", 1: "selectively admitted", 2: "filtered"}


class ChannelManager:
    def __init__(self, client: discord.Client, cfg: DiscordConfig, store: StateStore) -> None:
        self.client = client
        self.cfg = cfg
        self.store = store

    @property
    def guild(self) -> discord.Guild:
        guild = self.client.get_guild(self.cfg.guild_id)
        if guild is None:
            raise RuntimeError(f"bot is not in guild {self.cfg.guild_id}")
        return guild

    @staticmethod
    def slug(name: str) -> str:
        return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")[:90] or "channel"

    @staticmethod
    def topic_for(i3_channel: str, info: ChannelInfo | None) -> str:
        if info is None:
            return f'I3 channel "{i3_channel}" is no longer on the network.'
        kind = CHANNEL_TYPES.get(info.type, "unknown type")
        return (
            f'I3 channel "{i3_channel}" hosted by {info.host_mud} ({kind}). '
            "Messages here are relayed to every mud on the InterMUD-3 network."
        )[:1024]

    def text_channel(self, channel_id: int) -> discord.TextChannel | None:
        channel = self.guild.get_channel(channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    async def ensure_category(self) -> discord.CategoryChannel:
        wanted = self.cfg.category_name.lower()
        for category in self.guild.categories:
            if category.name.lower() == wanted:
                return category
        log.info("creating category %r", self.cfg.category_name)
        return await self.guild.create_category(self.cfg.category_name, reason="InterMUD-3 bridge")

    async def ensure_role(self, name: str) -> discord.Role:
        for role in self.guild.roles:
            if role.name.lower() == name.lower():
                return role
        log.info("creating role %r", name)
        return await self.guild.create_role(name=name, reason="InterMUD-3 bridge admin role")

    async def ensure_text_channel(self, name: str, topic: str) -> discord.TextChannel:
        existing = discord.utils.get(self.guild.text_channels, name=name)
        if existing is not None:
            return existing
        category = await self.ensure_category()
        log.info("creating #%s", name)
        return await self.guild.create_text_channel(
            name, category=category, topic=topic[:1024], reason="InterMUD-3 bridge"
        )

    async def _make_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        for webhook in await channel.webhooks():
            if webhook.name == WEBHOOK_NAME and webhook.token:
                return webhook
        return await channel.create_webhook(name=WEBHOOK_NAME, reason="InterMUD-3 bridge relay")

    async def join(self, i3_channel: str, info: ChannelInfo) -> ChannelLink:
        existing = self.store.link_for_i3(i3_channel)
        if existing is not None and self.text_channel(existing.discord_channel_id) is not None:
            return existing
        category = await self.ensure_category()
        name = f"{self.cfg.channel_prefix}{self.slug(i3_channel)}"[:100]
        channel = discord.utils.get(category.text_channels, name=name)
        if channel is not None and self.store.link_for_discord(channel.id) is not None:
            channel = None  # that channel already mirrors another I3 channel
        if channel is None:
            log.info("creating #%s for I3 channel %s", name, i3_channel)
            channel = await self.guild.create_text_channel(
                name,
                category=category,
                topic=self.topic_for(i3_channel, info),
                reason=f"InterMUD-3 bridge: joined {i3_channel}",
            )
        else:
            await channel.edit(topic=self.topic_for(i3_channel, info))
        webhook = await self._make_webhook(channel)
        link = ChannelLink(i3_channel, channel.id, webhook.id, webhook.token, "created")
        self.store.add_link(link)
        return link

    async def link_existing(
        self, i3_channel: str, channel: discord.TextChannel, info: ChannelInfo
    ) -> ChannelLink:
        if self.store.link_for_discord(channel.id) is not None:
            raise ValueError(f"#{channel.name} already mirrors an I3 channel")
        webhook = await self._make_webhook(channel)
        if not channel.topic:
            await channel.edit(topic=self.topic_for(i3_channel, info))
        link = ChannelLink(i3_channel, channel.id, webhook.id, webhook.token, "linked")
        self.store.add_link(link)
        return link

    async def unlink(self, i3_channel: str, *, delete: bool = False) -> str:
        link = self.store.link_for_i3(i3_channel)
        if link is None:
            raise ValueError(f"{i3_channel} is not linked to a Discord channel")
        channel = self.text_channel(link.discord_channel_id)
        if link.webhook_id is not None and link.webhook_token:
            webhook = discord.Webhook.partial(
                link.webhook_id, link.webhook_token, client=self.client
            )
            with contextlib.suppress(discord.HTTPException):
                await webhook.delete(reason="InterMUD-3 bridge: channel unlinked")
        self.store.remove_link(i3_channel)
        if channel is None:
            return f"Unlinked {i3_channel}; its Discord channel was already gone."
        if delete and link.mode == "created":
            await channel.delete(reason=f"InterMUD-3 bridge: left {i3_channel}")
            return f"Left {i3_channel} and deleted #{channel.name}."
        topic = f"[unlinked] {channel.topic or ''}"[:1024]
        await channel.edit(topic=topic)
        return f"Left {i3_channel}; #{channel.name} is kept but no longer relayed."

    async def recreate_webhook(self, link: ChannelLink) -> ChannelLink | None:
        channel = self.text_channel(link.discord_channel_id)
        if channel is None:
            return None
        try:
            webhook = await self._make_webhook(channel)
        except discord.HTTPException:
            log.exception("could not recreate webhook in #%s", channel.name)
            return None
        self.store.set_link_webhook(link.i3_channel, webhook.id, webhook.token)
        return ChannelLink(
            link.i3_channel, link.discord_channel_id, webhook.id, webhook.token, link.mode
        )

    async def reconcile(self, chanlist: dict[str, ChannelInfo]) -> list[tuple[int | None, str]]:
        """Bring Discord in line with persisted links. Returns (channel id, note) pairs."""
        notes: list[tuple[int | None, str]] = []
        for link in self.store.get_links():
            channel = self.text_channel(link.discord_channel_id)
            if channel is None:
                self.store.remove_link(link.i3_channel)
                notes.append(
                    (None, f"The Discord channel for I3 {link.i3_channel} is gone; unlinked it.")
                )
                continue
            info = chanlist.get(link.i3_channel)
            if link.mode == "created":
                topic = self.topic_for(link.i3_channel, info)
                if channel.topic != topic:
                    try:
                        await channel.edit(topic=topic)
                    except discord.HTTPException:
                        log.warning("could not update topic of #%s", channel.name)
            if link.webhook_id is None or not link.webhook_token:
                await self.recreate_webhook(link)
            if info is None:
                notes.append(
                    (channel.id, f"I3 channel {link.i3_channel} is no longer on the network.")
                )
        return notes
