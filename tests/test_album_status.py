"""Album status reports observed work and source admission is bounded."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.downloader import AlbumMedia
from bot.handlers import download


class AlbumStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_elapsed_phase_and_accepted_items_without_fake_percentage(self) -> None:
        status = download._AlbumStatus()
        status.started_at = time.monotonic() - 4
        self.assertIn("Получаю медиа поста", status.render())
        self.assertIn("4 с", status.render())
        status.phase = "prepare"
        self.assertIn("Проверяю медиа поста", status.render())
        status.phase = "upload"
        status.total = 3
        status.sent = 2
        self.assertIn("2/3 элементов", status.render())
        self.assertNotIn("%", status.render())

    async def test_album_source_uses_admission_and_stops_status_before_single_video(self) -> None:
        admitted = []
        with tempfile.TemporaryDirectory() as tmpdir:
            video = Path(tmpdir) / "post.mp4"
            video.write_bytes(b"video")
            album = AlbumMedia(items=[video], music=None)

            @asynccontextmanager
            async def admitted_media(_source, is_admin):
                admitted.append(is_admin)
                yield album

            async def single_video(*_args):
                self.assertEqual(
                    [task for task in asyncio.all_tasks()
                     if task.get_name() == "album-status"], []
                )
                return True

            with (
                patch.object(download, "_admitted_media", admitted_media),
                patch.object(download, "download_album", return_value=object()),
                patch.object(download, "_deliver_single_video", side_effect=single_video),
            ):
                result = await download._deliver_album(
                    MagicMock(), MagicMock(), MagicMock(),
                    "https://example.com/post", MagicMock(admin_id=1, cookies_file=None),
                    "instagram", 2, time.monotonic(), MagicMock(),
                )
        self.assertTrue(result)
        self.assertEqual(admitted, [False])


if __name__ == "__main__":
    unittest.main()
