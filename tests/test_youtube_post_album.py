"""YouTube photo posts must retain every attachment and clean up failures."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot import downloader


class YouTubeAlbumTests(unittest.TestCase):
    def test_downloads_originals_in_source_order(self):
        urls = ("https://yt3.ggpht.com/second=s0", "https://yt3.ggpht.com/first=s0")
        seen = []

        def stream(_session, url, destination, *_args, **kwargs):
            self.assertFalse(kwargs["allow_redirects"])
            seen.append(url)
            destination.write_bytes(url.encode())
            return True

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(downloader.youtube_posts, "resolve_post", return_value=
                         SimpleNamespace(image_urls=urls, description="caption")),
            patch.object(downloader, "_stream_to_file", side_effect=stream),
            patch.object(downloader, "_tikwm_photo_extension", return_value=".jpg"),
            patch("curl_cffi.requests.Session") as session,
        ):
            album = downloader._youtube_photo_album("unused", directory, "socks5h://proxy")
            self.assertEqual(seen, list(urls))
            self.assertEqual([p.read_text() for p in album.items], list(urls))
            self.assertEqual(album.description, "caption")
            session.return_value.close.assert_called_once()

    def test_missing_slide_rejects_partial_album(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(downloader.youtube_posts, "resolve_post", return_value=
                         SimpleNamespace(image_urls=("one", "two"))),
            patch.object(downloader, "_stream_to_file", side_effect=[True, False]),
            patch.object(downloader, "_tikwm_photo_extension", return_value=".jpg"),
            patch("curl_cffi.requests.Session") as session,
            patch.object(Path, "rename"),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_size=2)),
        ):
            with self.assertRaises(downloader.DownloadFailedError):
                downloader._youtube_photo_album("unused", directory, None)
            session.return_value.close.assert_called_once()


class YouTubeAlbumFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_uses_dedicated_extractor_and_cleans_directory(self):
        url = "https://www.youtube.com/post/Ugtest12345"
        directories = []

        def fetch(_url, directory, _proxy):
            directories.append(Path(directory))
            photo = Path(directory) / "slide.jpg"
            photo.write_bytes(b"photo")
            return downloader.AlbumMedia([photo], None)

        with (
            patch.object(downloader, "proxy_for", return_value=None),
            patch.object(downloader, "_album_exits", return_value=[None]),
            patch.object(downloader, "_youtube_photo_album", side_effect=fetch),
            patch.object(downloader, "_download_album_sync", new=MagicMock()) as gallery,
        ):
            async with downloader.download_album(url, None) as album:
                self.assertTrue(album.items[0].exists())
            gallery.assert_not_called()
        self.assertFalse(directories[0].exists())

    async def test_failed_exit_does_not_reuse_partial_slides(self):
        url = "https://www.youtube.com/post/Ugtest12345"
        directories = []

        def fetch(_url, directory, proxy):
            directories.append(Path(directory))
            photo = Path(directory) / "slide.jpg"
            if proxy == "broken":
                photo.write_bytes(b"partial")
                raise downloader.DownloadFailedError("network")
            self.assertFalse(photo.exists())
            photo.write_bytes(b"complete")
            return downloader.AlbumMedia([photo], None)

        with (
            patch.object(downloader, "proxy_for", return_value="broken"),
            patch.object(downloader, "_album_exits", return_value=["broken", "working"]),
            patch.object(downloader, "_youtube_photo_album", side_effect=fetch),
        ):
            async with downloader.download_album(url, None) as album:
                self.assertEqual(album.items[0].read_bytes(), b"complete")
        self.assertTrue(all(not directory.exists() for directory in directories))
