"""Posts I3 traffic into Discord through per-channel webhooks, with queues and coalescing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from ..state.db import ChannelLink

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
COALESCE_SECONDS = 1.0
MAX_CONTENT = 1950
QUEUE_LIMIT = 200
WEBHOOK_NAME = "InterMUD-3 bridge"


@dataclass(slots=True)
class _Item:
    link: ChannelLink
    username: str
    content: str
    avatar_url: str | None
    at: float


class _ChannelQueue:
    __slots__ = ("event", "items")

    def __init__(self) -> None:
        self.items: deque[_Item] = deque()
        self.event = asyncio.Event()


class Relay:
    def __init__(
        self,
        client: discord.Client,
        *,
        recreate_webhook: Callable[[ChannelLink], Awaitable[ChannelLink | None]],
    ) -> None:
        self.client = client
        self._recreate = recreate_webhook
        self._queues: dict[int, _ChannelQueue] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._links: dict[int, ChannelLink] = {}
        self.posted = 0
        self.dropped = 0
        self.failed = 0

    def post(
        self, link: ChannelLink, username: str, content: str, avatar_url: str | None = None
    ) -> None:
        cid = link.discord_channel_id
        q = self._queues.get(cid)
        if q is None:
            q = self._queues[cid] = _ChannelQueue()
            self._workers[cid] = asyncio.create_task(self._worker(cid, q), name=f"relay-{cid}")
        if len(q.items) >= QUEUE_LIMIT:
            self.dropped += 1
            log.warning("relay queue for channel %d full; dropping a message", cid)
            return
        q.items.append(
            _Item(self._links.get(cid, link), username, content, avatar_url, time.monotonic())
        )
        q.event.set()

    def queue_depth(self) -> int:
        return sum(len(q.items) for q in self._queues.values())

    async def stop(self) -> None:
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._workers.clear()

    async def notice(self, channel_id: int, text: str) -> None:
        await self._plain(channel_id, f":information_source: {text}")

    # ----------------------------------------------------------------- internals

    async def _worker(self, cid: int, q: _ChannelQueue) -> None:
        while True:
            await q.event.wait()
            q.event.clear()
            while q.items:
                first = q.items.popleft()
                batch = [first]
                total = len(first.content)
                while (
                    q.items
                    and q.items[0].username == first.username
                    and q.items[0].at - first.at <= COALESCE_SECONDS
                    and total + 1 + len(q.items[0].content) <= MAX_CONTENT
                ):
                    nxt = q.items.popleft()
                    batch.append(nxt)
                    total += 1 + len(nxt.content)
                content = "\n".join(i.content for i in batch)[:MAX_CONTENT]
                try:
                    await self._send(first.link, first.username, content, first.avatar_url)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failed += 1
                    log.exception("relay to channel %d failed", cid)

    async def _send(
        self, link: ChannelLink, username: str, content: str, avatar_url: str | None
    ) -> None:
        link = self._links.get(link.discord_channel_id, link)
        if link.webhook_id is None or not link.webhook_token:
            refreshed = await self._recreate(link)
            if refreshed is None or refreshed.webhook_id is None or not refreshed.webhook_token:
                await self._plain(link.discord_channel_id, f"**{username}**: {content}")
                return
            link = self._links[link.discord_channel_id] = refreshed
        assert link.webhook_id is not None and link.webhook_token is not None
        webhook = discord.Webhook.partial(link.webhook_id, link.webhook_token, client=self.client)
        try:
            await webhook.send(
                content,
                username=username,
                avatar_url=avatar_url,
                allowed_mentions=NO_MENTIONS,
                wait=False,
            )
            self.posted += 1
            return
        except discord.NotFound:
            log.warning("webhook for channel %d is gone; recreating", link.discord_channel_id)
        refreshed = await self._recreate(link)
        if refreshed is None or refreshed.webhook_id is None or not refreshed.webhook_token:
            await self._plain(link.discord_channel_id, f"**{username}**: {content}")
            return
        self._links[link.discord_channel_id] = refreshed
        webhook = discord.Webhook.partial(
            refreshed.webhook_id, refreshed.webhook_token, client=self.client
        )
        await webhook.send(
            content,
            username=username,
            avatar_url=avatar_url,
            allowed_mentions=NO_MENTIONS,
            wait=False,
        )
        self.posted += 1

    async def _plain(self, channel_id: int, text: str) -> None:
        channel = self.client.get_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            with contextlib.suppress(discord.HTTPException):
                await channel.send(text[:MAX_CONTENT], allowed_mentions=NO_MENTIONS)
