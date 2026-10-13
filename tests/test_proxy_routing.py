"""Explicit platform proxies work without unrelated xray pools."""

import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

from bot import proxy
from bot.handlers import admin


class ExplicitProxyTests(unittest.TestCase):
    def test_tiktok_explicit_primary_keeps_live_independent_fallback(self) -> None:
        router = proxy.ProxyRouter("socks5h://main:2080", "socks5h://goida:2079")
        router._routing = {"services": {"tiktok": "socks5h://warp:1080"}}
        router._pool_ok["main"] = False
        with (
            patch.object(proxy, "_router", router),
            patch.object(proxy, "_tiktok_own_vpn", return_value=False),
            patch.object(router, "_load_routing"),
        ):
            self.assertEqual(proxy.proxy_ladder("tiktok"), [
                "socks5h://warp:1080", "socks5h://goida:2079",
            ])

    def test_dedicated_warp_policy_without_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            routing = Path(directory) / "routing.toml"
            routing.write_text(
                '[defaults]\npolicy = "adaptive"\n'
                '[services]\njoidb = "socks5h://warp:1080"\n',
                encoding="utf-8",
            )
            with patch.object(proxy, "_ROUTING_FILE", routing):
                router = proxy.ProxyRouter(None)
                self.assertFalse(router.enabled)
                self.assertEqual(router.proxy_for("joidb"), "socks5h://warp:1080")
                self.assertIsNone(router.proxy_for("youtube"))

    def test_control_marks_warp_backed_pool_health_as_unverified(self) -> None:
        with patch.dict(environ, {"XRAY_WARP_FALLBACK": "1"}):
            self.assertTrue(proxy.warp_fallback_enabled())
            self.assertIn("состояние собственных VPN-узлов отдельно не подтверждено",
                          admin._control_text())
        with patch.dict(environ, {"XRAY_WARP_FALLBACK": "0"}):
            self.assertFalse(proxy.warp_fallback_enabled())
            self.assertNotIn("Резерв WARP включён", admin._control_text())


class PoolProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_exit_is_not_healthy(self) -> None:
        app = web.Application()

        async def limited(_request):
            return web.Response(status=429)

        app.router.add_get("/", limited)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            port = site._server.sockets[0].getsockname()[1]
            self.assertFalse(await proxy.ProxyRouter._reachable(
                f"http://127.0.0.1:{port}/", None,
            ))
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
