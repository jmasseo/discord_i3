"""Process entry for ``i3bridge run``: Discord bot + I3 client + health server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

import discord

from ..config import Config
from ..discord_.bot import BridgeBot
from ..health import HealthServer
from ..state.db import StateStore
from .core import Bridge

log = logging.getLogger(__name__)


async def _forever() -> None:
    await asyncio.Event().wait()


async def run_bridge(cfg: Config, store: StateStore, *, with_i3: bool = True) -> int:
    if not cfg.discord_token:
        log.error("DISCORD_TOKEN is not set")
        return 2
    if not cfg.discord.guild_id:
        log.error("discord.guild_id is not set in the config")
        return 2

    bot = BridgeBot(cfg)
    bridge = Bridge(cfg, store, bot)
    bot.bridge = bridge
    health = HealthServer(
        cfg.health.bind,
        cfg.health.port,
        ready=lambda: bridge.ready,
        metrics=bridge.metrics,
        details=bridge.details,
    )
    await health.start()
    log.info("health endpoint on http://%s:%d/health", cfg.health.bind, cfg.health.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    if with_i3:
        i3_task = asyncio.create_task(bridge.client.run(), name="i3-client")
    else:
        log.warning("--no-i3: not connecting to the router; Discord side only")
        i3_task = asyncio.create_task(_forever(), name="i3-disabled")
    bot_task = asyncio.create_task(bot.start(cfg.discord_token), name="discord-bot")
    stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
    exit_code = 0
    try:
        done, _ = await asyncio.wait(
            {i3_task, bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if bot_task in done:
            exc = bot_task.exception()
            if isinstance(exc, discord.PrivilegedIntentsRequired):
                log.error(
                    "Enable the Presence, Server Members and Message Content intents for the bot "
                    "in the Discord developer portal, then restart."
                )
                exit_code = 3
            elif isinstance(exc, discord.LoginFailure):
                log.error("Discord rejected the token: %s", exc)
                exit_code = 3
            elif exc is not None:
                log.error("Discord client stopped: %r", exc)
                exit_code = 1
        elif i3_task in done and i3_task.exception() is not None:
            log.error("I3 client stopped: %r", i3_task.exception())
            exit_code = 1
        else:
            log.info("shutting down")
    finally:
        bridge.client.stop()
        if not with_i3:
            i3_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(i3_task, 10)
        await bridge.relay.stop()
        if not bot.is_closed():
            with contextlib.suppress(Exception):
                await bot.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(bot_task, 10)
        stop_task.cancel()
        await health.stop()
    return exit_code
