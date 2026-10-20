"""Tiny aiohttp server backing the Docker healthcheck."""

from __future__ import annotations

from aiohttp import web


async def start_health_server(port: int, db=None, admin_id: int | None = None) -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application(client_max_size=4096)
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    if db is not None and admin_id is not None:
        from bot.dashboard import register_dashboard

        register_dashboard(app, db, admin_id)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    return runner
