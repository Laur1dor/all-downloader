"""Resolved audio searches must retain the real source's proxy policy."""

import unittest
from unittest.mock import patch

from bot.downloader import _base_options


class AudioSearchRoutingTests(unittest.TestCase):
    def test_internal_searches_use_youtube_route(self):
        for query in ("ytsearch1:artist track", "ytsearch5:artist track", "ytsearch:track"):
            with self.subTest(query=query), patch(
                "bot.downloader.proxy_for", return_value="socks5h://proxy:2068",
            ) as route:
                options = _base_options(None, query)
            route.assert_called_once_with("youtube")
            self.assertEqual(options["proxy"], "socks5h://proxy:2068")

    def test_forced_search_uses_youtube_policy(self):
        with patch("bot.downloader.forced_proxy", return_value="socks5h://proxy:2080") as route:
            options = _base_options(None, "ytsearch1:artist track", force_proxy=True)
        route.assert_called_once_with("youtube")
        self.assertEqual(options["proxy"], "socks5h://proxy:2080")

    def test_normal_service_url_keeps_its_policy(self):
        with patch("bot.downloader.proxy_for", return_value=None) as route:
            options = _base_options(None, "https://soundcloud.com/artist/track")
        route.assert_called_once_with("soundcloud")
        self.assertNotIn("proxy", options)

    def test_unrelated_synthetic_input_is_not_youtube(self):
        with patch("bot.downloader.proxy_for", return_value=None) as route:
            _base_options(None, "othersearch1:track")
        route.assert_called_once_with("other")
