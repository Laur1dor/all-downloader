"""Cancellation must leave a running downloader's directory intact."""

import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import downloader


class DownloadCancellationCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_capped_writer_can_finish_after_cancel(self):
        loop = asyncio.get_running_loop()
        started = loop.create_future()
        release = threading.Event()
        cancelled = threading.Event()
        wrote_after_cancel = threading.Event()

        def download(_url, options):
            directory = Path(options["outtmpl"]).parent
            loop.call_soon_threadsafe(started.set_result, directory)
            if not release.wait(5):
                raise RuntimeError("Test writer was not released")
            output = directory / "finished.mp4"
            output.write_bytes(b"media")
            wrote_after_cancel.set()
            return output, {}

        async def consume():
            async with downloader._temporary_download(
                "https://www.youtube.com/watch?v=abcdefghijk",
                {}, "best", "cancel-regression-", 1000, cancelled, {}, True,
            ):
                self.fail("Cancelled download yielded media")

        with patch.object(downloader, "_download_sync", download):
            task = asyncio.create_task(consume())
            try:
                directory = await asyncio.wait_for(started, 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(cancelled.is_set())
                self.assertTrue(directory.exists())
            finally:
                release.set()
            async with asyncio.timeout(3):
                while directory.exists():
                    await asyncio.sleep(0.01)
            self.assertTrue(wrote_after_cancel.is_set())
