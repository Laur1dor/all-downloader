"""Tiny aiohttp server backing the Docker healthcheck."""

from __future__ import annotations

from aiohttp import web


async def start_health_server(port: int) -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    return runner
