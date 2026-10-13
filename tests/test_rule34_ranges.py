"""Both Rule34 sites must preserve the requested excerpt through delivery."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot import downloader
from bot.handlers import download
from bot.progress import CancelRegistry
from bot.urlcache import UrlCache


class Rule34RangeTests(unittest.IsolatedAsyncioTestCase):
    def test_rule34video_preserves_local_trimming_for_socks_transport(self) -> None:
        url = "https://rule34video.com/video/123/example/"
        marker = object()
        with patch.object(downloader, "_temporary_download", return_value=marker) as temporary, \
                patch.object(downloader, "_ranged_download") as ranged:
            self.assertIs(downloader.download_video(url, None, time_range=(10, 20)), marker)
        options = temporary.call_args.args[1]
        self.assertNotIn("download_ranges", options)
        ranged.assert_not_called()

    async def test_rule34_uses_scraper_and_keeps_source_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rule34.webm"
            source.write_bytes(b"video")
            cleaned = False

            @asynccontextmanager
            async def album(*_args):
                nonlocal cleaned
                try:
                    yield downloader.AlbumMedia([source], None)
                finally:
                    cleaned = True

            with patch.object(downloader, "download_album", album), \
                    patch.object(downloader, "video_metadata", return_value=(640, 360, 30)), \
                    patch.object(downloader, "_temporary_download") as ytdlp:
                async with downloader.download_video(
                    "https://rule34.xxx/index.php?page=post&s=view&id=123", None,
                    time_range=(10, 20),
                ) as media:
                    self.assertEqual(media.path, source)
                    self.assertEqual(media.duration, 30)
                    self.assertFalse(cleaned)
                self.assertTrue(cleaned)
                ytdlp.assert_not_called()

    async def test_rule34_photo_and_oversized_source_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for extension, limit, error in (
                (".png", 100, downloader.DownloadFailedError),
                (".mp4", 1, downloader.OversizedError),
            ):
                with self.subTest(extension=extension):
                    source = Path(directory) / f"post{extension}"
                    source.write_bytes(b"media")

                    @asynccontextmanager
                    async def album(*_args, source=source):
                        yield downloader.AlbumMedia([source], None)

                    with patch.object(downloader, "download_album", album), \
                            self.assertRaises(error):
                        async with downloader.download_video(
                            "https://rule34.xxx/index.php?page=post&s=view&id=123", None,
                            max_bytes=limit, time_range=(10, 20),
                        ):
                            self.fail("invalid source delivered")

    async def test_handler_trims_both_rule34_sites_without_caching_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.mp4"
            source.write_bytes(b"video")
            for platform, url, expected_range in (
                ("rule34", "https://rule34.xxx/index.php?page=post&s=view&id=123", (10, 20)),
                ("rule34video", "https://rule34video.com/video/123/example/", (10, 20)),
            ):
                with self.subTest(platform=platform), ExitStack() as stack:
                    message = MagicMock(
                        text=f"{url} 00:10 — 00:20",
                        from_user=SimpleNamespace(id=1, username="tester"),
                    )
                    message.answer = AsyncMock(return_value=MagicMock())
                    db = MagicMock(
                        upsert_user=AsyncMock(), store_cached_file=AsyncMock(),
                        record_upload=AsyncMock(),
                    )
                    settings = SimpleNamespace(admin_id=1, cookies_file=None)
                    media = downloader.Media(source, "test", 5, 30, 640, 360)

                    @asynccontextmanager
                    async def video(*_args, media=media, **_kwargs):
                        yield media

                    patches = {
                        "expand_short_link": AsyncMock(return_value=url),
                        "resolve_download_url": AsyncMock(return_value=url),
                        "ensure_public_url": MagicMock(),
                        "_download_limit": MagicMock(return_value=100 * 1024**2),
                        "_part_upload_limit": MagicMock(return_value=100),
                        "_ratelimit_bps": MagicMock(return_value=None),
                        "download_video": MagicMock(side_effect=video),
                        "prepare_video": AsyncMock(return_value=[source]),
                        "_select_quality": AsyncMock(return_value=downloader.QualityOption(
                            360, None, "mp4-360",
                        )),
                        "video_metadata": MagicMock(return_value=(640, 360, 10)),
                        "_safe_edit": AsyncMock(),
                        "_edit_progress_loop": AsyncMock(),
                        "_send_prepared_part": AsyncMock(return_value=SimpleNamespace(
                            video=SimpleNamespace(file_id="excerpt"), document=None)),
                        "_record": AsyncMock(),
                        "_terminal_delete": AsyncMock(),
                    }
                    for name, replacement in patches.items():
                        stack.enter_context(patch.object(download, name, replacement))
                    await download._run_link(message, db, settings, UrlCache(), CancelRegistry())
                    self.assertEqual(patches["prepare_video"].await_args.args[2], expected_range)
                    self.assertEqual(patches["download_video"].call_args.kwargs["time_range"],
                                     (10, 20))
                    patches["_select_quality"].assert_awaited_once()
                    self.assertNotIn("max_height", patches["prepare_video"].await_args.kwargs)
                    self.assertEqual(
                        patches["download_video"].call_args.kwargs["format_override"],
                        downloader.quality_selector(downloader.QualityOption(360, None, "mp4-360"))
                        if platform == "rule34video" else None,
                    )
                    patches["_send_prepared_part"].assert_awaited_once()
                    self.assertEqual(patches["_record"].await_args.args[2], "done")
                    db.store_cached_file.assert_not_awaited()

    async def test_cancelled_rule34_does_not_fetch_source(self) -> None:
        cancel = Event()
        cancel.set()
        with patch.object(downloader, "download_album") as album, \
                self.assertRaises(downloader.DownloadCancelledError):
            async with downloader.download_video(
                "https://rule34.xxx/index.php?page=post&s=view&id=123", None,
                time_range=(10, 20), cancel_event=cancel,
            ):
                self.fail("cancelled source delivered")
        album.assert_not_called()


if __name__ == "__main__":
    unittest.main()
