"""JOI player pages can transiently omit their HLS source."""

import unittest
from unittest.mock import patch

from bot import downloader


class JoiResolverTests(unittest.TestCase):
    def test_retries_a_successful_page_without_player(self) -> None:
        url = "https://www.the-joi-database.com/watch/example"
        with (
            patch.object(
                downloader, "_fetch_html",
                side_effect=["<html><body>Loading</body></html>",
                             '<div data-hls-source="/api/stream/example"></div>'],
            ) as fetch,
            patch.object(downloader.time, "sleep") as sleep,
        ):
            resolved = downloader._resolve_joidb_sync(url, "socks5h://warp:1080")
        self.assertEqual(
            resolved, "https://www.the-joi-database.com/api/stream/example",
        )
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_stops_after_two_shell_pages(self) -> None:
        with (
            patch.object(downloader, "_fetch_html", return_value="<html></html>") as fetch,
            patch.object(downloader.time, "sleep"),
            self.assertRaises(downloader.DownloadFailedError),
        ):
            downloader._resolve_joidb_sync(
                "https://www.the-joi-database.com/watch/example", None,
            )
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
