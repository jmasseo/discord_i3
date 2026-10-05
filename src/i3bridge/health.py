"""Liveness, readiness and Prometheus metrics over HTTP (aiohttp)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from aiohttp import web


class HealthServer:
    def __init__(
        self,
        bind: str,
        port: int,
        *,
        ready: Callable[[], bool],
        metrics: Callable[[], Mapping[str, float]],
        details: Callable[[], Mapping[str, object]],
    ) -> None:
        self.bind = bind
        self.port = port
        self._ready = ready
        self._metrics = metrics
        self._details = details
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/health/ready", self._readiness)
        app.router.add_get("/metrics", self._prometheus)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.bind, self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _health(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok", **dict(self._details())})

    async def _readiness(self, _: web.Request) -> web.Response:
        ready = self._ready()
        return web.json_response(
            {"ready": ready, **dict(self._details())}, status=200 if ready else 503
        )

    async def _prometheus(self, _: web.Request) -> web.Response:
        lines = [f"i3bridge_{name} {value}" for name, value in sorted(self._metrics().items())]
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")
