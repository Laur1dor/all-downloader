"""A stalled Rule34Video CDN must not be reported as an oversized file."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp.utils import DownloadError

from bot import downloader, proxy, rule34video


class Rule34VideoMenuTests(unittest.TestCase):
    def test_svg_sizes_entities_and_attribute_order(self) -> None:
        webpage = '''
        <a class="tag_item_download" href="/get_file/1/clip_1080p.mp4/?download=true&amp;token=x">
          <svg><path d="M11 3"/></svg>MP4 1080p<span class="vp-sheet__meta">28 MB</span>
        </a>
        <a data-session="PHPSESSID" role="menuitem"
           href="https://rule34video.com/get_file/1/clip_720p.mp4/?token=y&amp;download=true">
          <svg></svg>MP4 720p<span>16 MB</span></a>
        <a href="/get_file/1/clip_480p.webm/?download=true">WEBM 480p</a>
        '''
        with patch.object(rule34video, "ensure_public_url") as check:
            formats = rule34video.download_formats(webpage, "https://rule34video.com/video/1/")
        self.assertEqual([f["height"] for f in formats], [1080, 720, 480])
        self.assertEqual(
            formats[0]["url"],
            "https://rule34video.com/get_file/1/clip_1080p.mp4/?download=true&token=x",
        )
        self.assertEqual(formats[2]["ext"], "webm")
        self.assertEqual(check.call_count, 3)

    def test_untrusted_links_are_not_formats(self) -> None:
        urls = (
            "https://127.0.0.1/get_file/1/?download=true",
            "https://rule34video.com.evil.test/get_file/1/?download=true",
            "https://user@rule34video.com/get_file/1/?download=true",
            "https://rule34video.com:80/get_file/1/?download=true",
            "https://rule34video.com:invalid/get_file/1/?download=true",
            "http://rule34video.com/get_file/1/?download=true",
            "https://rule34video.com/video/1/?download=true",
            "https://rule34video.com/get_file/1/?download=false",
        )
        webpage = ''.join(f'<a href="{url}">MP4 1080p</a>' for url in urls)
        with patch.object(rule34video, "ensure_public_url") as check:
            self.assertEqual(
                rule34video.download_formats(webpage, "https://rule34video.com/video/1/"), [],
            )
        check.assert_not_called()

    def test_duplicate_menu_links_are_deduplicated(self) -> None:
        link = '<a href="/get_file/1/clip.mp4/?download=true">MP4 720p</a>'
        with patch.object(rule34video, "ensure_public_url"):
            self.assertEqual(
                len(rule34video.download_formats(link * 2, "https://rule34video.com/video/1/")), 1,
            )

    def test_extractor_fetches_once_and_preserves_metadata(self) -> None:
        page = (
            '<title>Example</title><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"VideoObject",'
            '"name":"Example","duration":"PT0H0M51S"}</script>'
            '<a href="/get_file/1/clip.mp4/?download=true">'
            '<svg></svg>MP4 720p<span>16 MB</span></a>'
        )
        with (
            patch.object(
                rule34video._Rule34VideoIE, "_download_webpage", return_value=page,
            ) as fetch,
            patch.object(rule34video, "ensure_public_url"),
            downloader.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl,
        ):
            ydl.add_info_extractor(rule34video.Rule34VideoIE())
            info = ydl.extract_info("https://rule34video.com/video/1/", download=False)
        fetch.assert_called_once()
        self.assertEqual(info["title"], "Example")
        self.assertEqual(info["age_limit"], 18)
        self.assertEqual(info["duration"], 51)
        self.assertEqual(info["formats"][0]["height"], 720)


class CappedDownloadErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_error_is_preserved_without_size_guard(self) -> None:
        source_error = DownloadError("SSL: UNEXPECTED_EOF_WHILE_READING")
        with (
            patch.object(downloader.asyncio, "to_thread", side_effect=source_error),
            self.assertRaises(DownloadError) as caught,
        ):
            await downloader._download_capped(
                "https://rule34video.com/video/1/", {}, "best", 100,
                {"tripped": None},
            )
        self.assertIs(caught.exception, source_error)
        self.assertTrue(downloader._is_block_error(caught.exception))

    async def test_actual_size_guard_stays_oversized(self) -> None:
        with (
            patch.object(
                downloader.asyncio, "to_thread",
                side_effect=DownloadError("download aborted by hook"),
            ),
            self.assertRaises(downloader.FileTooLargeError) as caught,
        ):
            await downloader._download_capped(
                "https://rule34video.com/video/1/", {}, "best", 100,
                {"tripped": 120},
            )
        self.assertEqual(caught.exception.size_bytes, 120)

    async def test_capped_network_error_requests_alternate_exit(self) -> None:
        source_error = DownloadError("SSL: UNEXPECTED_EOF_WHILE_READING")
        with (
            patch.object(downloader, "_download_sync", side_effect=source_error),
            self.assertRaises(downloader.DownloadFailedError) as caught,
        ):
            async with downloader._temporary_download(
                "https://rule34video.com/video/1/", {}, "best", "r34-test-",
                100, None, {"tripped": None}, True,
            ):
                self.fail("The failed download must not yield media")
        self.assertNotIsInstance(caught.exception, downloader.FileTooLargeError)
        self.assertTrue(caught.exception.retry_via_proxy)

    async def test_cdn_transport_failures_request_alternate_exit(self) -> None:
        for capped in (False, True):
            for message in (
                "The read operation timed out",
                "Connection reset by peer",
                "Remote end closed connection without response",
                "IncompleteRead(100 bytes read, 200 more expected)",
                "SSL: WRONG_VERSION_NUMBER",
            ):
                with self.subTest(capped=capped, message=message):
                    with (
                        patch.object(downloader, "_probe_filesize", return_value=None),
                        patch.object(
                            downloader, "_download_sync",
                            side_effect=DownloadError(message),
                        ),
                        self.assertRaises(downloader.DownloadFailedError) as caught,
                    ):
                        async with downloader._temporary_download(
                            "https://rule34video.com/video/1/", {}, "best",
                            "r34-test-", 100, None, {"tripped": None}, capped,
                        ):
                            self.fail("The failed download must not yield media")
                    self.assertNotIsInstance(caught.exception, downloader.FileTooLargeError)
                    self.assertTrue(caught.exception.retry_via_proxy)

    async def test_non_transport_failure_does_not_request_alternate_exit(self) -> None:
        with (
            patch.object(
                downloader, "_download_sync",
                side_effect=DownloadError("Requested format is not available"),
            ),
            self.assertRaises(downloader.DownloadFailedError) as caught,
        ):
            async with downloader._temporary_download(
                "https://rule34video.com/video/1/", {}, "best", "r34-test-",
                100, None, {"tripped": None}, True,
            ):
                self.fail("The failed download must not yield media")
        self.assertFalse(caught.exception.retry_via_proxy)

    def test_transport_retry_is_specific_to_rule34video(self) -> None:
        self.assertFalse(downloader._retry_via_proxy(
            DownloadError("The read operation timed out"),
            "https://www.youtube.com/watch?v=123",
        ))


class Rule34VideoExitTests(unittest.TestCase):
    def test_warp_cdn_retry_uses_live_personal_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            routing = Path(directory) / "routing.toml"
            routing.write_text(
                '[defaults]\npolicy = "adaptive"\n[services]\n'
                'rule34video = "socks5h://warp:1080"\n'
                'joidb = "socks5h://warp:1080"\n',
                encoding="utf-8",
            )
            with patch.object(proxy, "_ROUTING_FILE", routing):
                router = proxy.ProxyRouter(
                    "socks5h://xray:2077", "socks5h://xray:2079",
                )
                self.assertEqual(
                    router.forced_proxy("rule34video"), "socks5h://xray:2077",
                )
                self.assertEqual(
                    router.forced_proxy("joidb"), "socks5h://xray:2079",
                )

                with patch.object(proxy, "_router", router):
                    url = "https://rule34video.com/video/1/"
                    self.assertEqual(
                        downloader._base_options(None, url)["proxy"],
                        "socks5h://warp:1080",
                    )
                    self.assertEqual(
                        downloader._base_options(None, url, force_proxy=True)["proxy"],
                        "socks5h://xray:2077",
                    )


if __name__ == "__main__":
    unittest.main()
