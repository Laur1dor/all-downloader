"""Short-link redirects must preserve TikTok routing and a finite wait."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.downloader import expand_short_link


class TikTokShortLinkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        guard = patch("bot.urlguard.ensure_public_url")
        guard.start()
        self.addCleanup(guard.stop)

    async def test_unrelated_redirect_does_not_replace_tiktok_post(self):
        original = "https://vt.tiktok.com/example/"
        post = "https://www.tiktok.com/@example/video/123"
        with (
            patch("bot.downloader.proxy_ladder", return_value=["first", "second"]),
            patch("curl_cffi.requests.head", side_effect=[
                SimpleNamespace(status_code=302, headers={"Location": "https://www.tt.site/"}),
                SimpleNamespace(status_code=302, headers={"Location": post}),
            ]) as head,
        ):
            self.assertEqual(await expand_short_link(original), post)
            self.assertEqual(head.call_count, 2)
            self.assertTrue(all(call.args[0] == original for call in head.call_args_list))
            self.assertFalse(head.call_args.kwargs["allow_redirects"])

    async def test_landing_page_keeps_original_link(self):
        original = "https://vt.tiktok.com/example/"
        with (
            patch("bot.downloader.proxy_ladder", return_value=[None]),
            patch("curl_cffi.requests.head", return_value=SimpleNamespace(
                status_code=302, headers={"Location": "https://www.tiktok.com/login"})),
        ):
            self.assertEqual(await expand_short_link(original), original)

    async def test_login_redirect_unwraps_photo_post(self):
        post = "https://www.tiktok.com/@example/photo/123"
        with (
            patch("bot.downloader.proxy_ladder", return_value=[None]),
            patch("curl_cffi.requests.head", return_value=SimpleNamespace(
                status_code=302,
                headers={"Location": "https://www.tiktok.com/login?redirect_url=" + post})),
        ):
            self.assertEqual(await expand_short_link("https://vt.tiktok.com/example/"), post)

    async def test_dead_routes_stop_at_shared_budget(self):
        original = "https://vt.tiktok.com/example/"
        clock = SimpleNamespace(now=100)

        def fail(*args, **kwargs):
            clock.now += 8
            raise TimeoutError

        with (
            patch("bot.downloader.proxy_ladder", return_value=["a", "b", "c", "d"]),
            patch("bot.downloader.time", SimpleNamespace(monotonic=lambda: clock.now)),
            patch("curl_cffi.requests.head", side_effect=fail) as head,
        ):
            self.assertEqual(await expand_short_link(original), original)
            self.assertEqual(head.call_count, 4)
            self.assertEqual(head.call_args.kwargs["timeout"], 6)
