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


class InstagramFallbackTests(unittest.TestCase):
    def test_failed_pinned_exit_has_distinct_live_album_and_video_fallbacks(self):
        from bot.downloader import _album_exits
        router=proxy.ProxyRouter('socks5h://main:2080','socks5h://goida:2079','socks5h://bypass:2078')
        router._routing={'services':{'instagram':'socks5h://warp:1080'}}
        router._pool_ok['bypass']=False
        with patch.object(proxy,'_router',router),patch.object(router,'_load_routing'):
            expected=['socks5h://warp:1080','socks5h://goida:2079','socks5h://main:2080']
            self.assertEqual(proxy.proxy_ladder('instagram'),expected)
            self.assertEqual(_album_exits('instagram',expected[0]),expected)



class InstagramNetworkRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_request_exception_moves_album_to_next_route(self):
        from bot import downloader
        from curl_cffi.requests.exceptions import SSLError
        calls=[]
        def fetch(url,tmpdir,exit_proxy,*args):
            calls.append(exit_proxy)
            if len(calls)==1:
                (Path(tmpdir)/'partial.jpg').write_bytes(b'partial')
                raise SSLError('SSL_ERROR_SYSCALL')
            self.assertFalse((Path(tmpdir)/'partial.jpg').exists())
            image=Path(tmpdir)/'image.jpg';image.write_bytes(b'complete')
            return [image],'caption'
        with patch.object(downloader,'proxy_for',return_value='warp'), \
             patch.object(downloader,'proxy_ladder',return_value=['warp','goida']), \
             patch.object(downloader,'_instagram_items_sync',side_effect=fetch):
            async with downloader.download_album('https://www.instagram.com/p/DfSJ06iEZ5I/',None) as album:
                self.assertEqual(album.items[0].read_bytes(),b'complete')
        self.assertEqual(calls,['warp','goida'])
