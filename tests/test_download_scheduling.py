"""A slow upload must not occupy a source-download slot."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from aiogram.types import FSInputFile

from bot import downloader
from bot.handlers import download
from bot.progress import PHASE_PREPARE, PHASE_QUEUE, ProgressState, render_progress


class DownloadSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def test_multipart_release_preserves_all_upload_files_and_single_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            parts = [root / "part-01.mp4", root / "part-02.mp4"]
            for path in [source, *parts]:
                path.write_bytes(b"video")

            download._release_source_after_split(source, parts)
            self.assertFalse(source.exists())
            self.assertTrue(all(path.exists() for path in parts))

            single = root / "single.mp4"
            single.write_bytes(b"video")
            download._release_source_after_split(single, [single])
            self.assertTrue(single.exists())

            # If a future preparer returns the source among output paths, it
            # must remain readable while that part is being uploaded.
            download._release_source_after_split(single, [single, *parts])
            self.assertTrue(single.exists())

            absent = root / "missing.mp4"
            download._release_source_after_split(single, [*parts, absent])
            self.assertTrue(single.exists())

    async def test_each_accepted_part_is_released_before_next_upload(self) -> None:
        with TemporaryDirectory() as directory:
            parts = [Path(directory) / "part-01.mp4", Path(directory) / "part-02.mp4"]
            for part in parts:
                part.write_bytes(b"video")
            calls = 0

            async def accepted(build, bot, _timeout):
                nonlocal calls
                if calls == 0:
                    self.assertTrue(parts[0].exists())
                elif calls == 1:
                    self.assertFalse(parts[0].exists())
                    self.assertTrue(parts[1].exists())
                payload = b"".join([chunk async for chunk in build().read(bot)])
                self.assertEqual(payload, b"video")
                calls += 1
                return SimpleNamespace(video=object())

            with patch.object(download, "_send_with_retries", side_effect=accepted):
                for part in parts:
                    await download._send_prepared_part(
                        lambda path=part: FSInputFile(path), None, 480, part, True,
                    )
            self.assertEqual(calls, 2)
            self.assertFalse(any(part.exists() for part in parts))

            single = Path(directory) / "single.mp4"
            single.write_bytes(b"video")
            with patch.object(download, "_send_with_retries", side_effect=accepted):
                await download._send_prepared_part(
                    lambda: FSInputFile(single), None, 480, single, False,
                )
            self.assertTrue(single.exists())

            failed = Path(directory) / "failed.mp4"
            failed.write_bytes(b"video")
            with patch.object(
                download, "_send_with_retries", side_effect=RuntimeError("uncertain"),
            ), self.assertRaisesRegex(RuntimeError, "uncertain"):
                await download._send_prepared_part(
                    lambda: None, None, 480, failed, True,
                )
            self.assertTrue(failed.exists())

    async def test_known_small_quality_proceeds_while_large_job_is_active(self) -> None:
        size = 20 * 1024**2
        part_limit = 768 * 1024**2

        @asynccontextmanager
        async def source():
            yield SimpleNamespace(file_size=size)

        async def small():
            async with download._admitted_media(
                source(), False,
                large_source=download._needs_large_video_slot(True, None, size, part_limit),
                large_if_bytes=part_limit,
            ) as media:
                self.assertEqual(media.file_size, size)

        with patch.object(download, "_large_source_slots", asyncio.Semaphore(0)), \
             patch.object(download, "_download_slots", asyncio.Semaphore(1)):
            await asyncio.wait_for(small(), 1)

    async def test_slot_is_released_before_media_context_exits(self) -> None:
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release_first = asyncio.Event()

        @asynccontextmanager
        async def source(number: int):
            if number == 1:
                first_entered.set()
            else:
                second_entered.set()
            yield number

        async def first():
            async with download._admitted_media(source(1), False):
                await release_first.wait()

        async def second():
            await first_entered.wait()
            async with download._admitted_media(source(2), False):
                pass

        with patch.object(download, "_download_slots", asyncio.Semaphore(1)):
            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            try:
                await asyncio.wait_for(second_entered.wait(), 1)
            finally:
                release_first.set()
                await asyncio.gather(first_task, second_task)

    async def test_large_sources_have_finite_queue_while_small_source_proceeds(self) -> None:
        release_first = asyncio.Event()
        first_entered = asyncio.Event()
        small_entered = asyncio.Event()

        @asynccontextmanager
        async def source(number: int):
            if number == 1:
                first_entered.set()
            if number == 0:
                small_entered.set()
            yield number

        async def large(number: int):
            async with download._admitted_media(source(number), True, large_source=True):
                if number == 1:
                    await release_first.wait()

        async def small():
            async with download._admitted_media(source(0), False):
                pass

        with patch.object(download, "_large_source_slots", asyncio.Semaphore(1)), \
             patch.object(download, "_large_source_waiters", 0):
            first = asyncio.create_task(large(1))
            await asyncio.wait_for(first_entered.wait(), 1)
            second = asyncio.create_task(large(2))
            third = asyncio.create_task(large(3))
            try:
                for _ in range(100):
                    if download._large_source_waiters == 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(download._large_source_waiters, 2)
                with self.assertRaisesRegex(downloader.DownloadFailedError, "Очередь"):
                    async with download._admitted_media(source(4), True, large_source=True):
                        self.fail("a fourth large source entered the bounded queue")
                await asyncio.wait_for(small(), 1)
                self.assertTrue(small_entered.is_set())
            finally:
                release_first.set()
                await asyncio.gather(first, second, third)
            self.assertEqual(download._large_source_waiters, 0)

    async def test_unexpected_large_short_video_does_not_wait_with_source_on_disk(self) -> None:
        cleaned = []

        @asynccontextmanager
        async def source(size: int):
            try:
                yield SimpleNamespace(file_size=size)
            finally:
                cleaned.append(size)

        large_gate = asyncio.Semaphore(1)
        await large_gate.acquire()
        with patch.object(download, "_large_source_slots", large_gate):
            with self.assertRaisesRegex(downloader.DownloadFailedError, "Очередь"):
                async with download._admitted_media(
                    source(800), False, large_if_bytes=768,
                ):
                    self.fail("unexpected large source was retained")
            async with download._admitted_media(
                source(100), False, large_if_bytes=768,
            ):
                pass
        self.assertEqual(cleaned, [800, 100])

    def test_network_part_limit_matches_upload_deadline(self) -> None:
        settings = SimpleNamespace(admin_id=1, max_upload_bytes=2000 * 1024**2)
        with patch.object(download.config, "upload_bytes", return_value=2000 * 1024**2), \
             patch.object(download.config, "get", return_value=100):
            self.assertEqual(download._part_upload_limit(settings, 2), 768 * 1024**2)
        with patch.object(download.config, "upload_bytes", return_value=2000 * 1024**2), \
             patch.object(download.config, "get", return_value=10):
            self.assertEqual(download._part_upload_limit(settings, 2), 384 * 1024**2)
        self.assertLess(download._upload_timeout(768 * 1024**2), 500)

    def test_unknown_download_and_preparation_show_honest_state(self) -> None:
        progress = ProgressState()
        with patch("bot.progress.time.monotonic", return_value=progress.started_at + 8):
            text = render_progress(progress, "видео")
        self.assertIn("ожидаю данные источника · 8 с", text)
        self.assertNotIn("0.0 МБ", text)
        progress.phase = PHASE_PREPARE
        with patch("bot.progress.time.monotonic", return_value=progress.started_at + 3):
            self.assertIn("Готовлю видео… 3 с", render_progress(progress, "видео"))
        progress.phase = PHASE_QUEUE
        with patch("bot.progress.time.monotonic", return_value=progress.started_at + 5):
            self.assertIn("Ожидаю очередь для видео… 5 с", render_progress(progress, "видео"))

    def test_download_aborts_when_disk_floor_is_reached(self) -> None:
        guard = {"limit": 1024**3, "tripped": None}
        with patch("bot.downloader.shutil.disk_usage") as usage:
            usage.return_value.free = 100 * 1024**2
            with self.assertRaisesRegex(
                downloader.DownloadFailedError, "свободного места"
            ):
                downloader._progress_hook(None, None, guard)({
                    "status": "downloading", "downloaded_bytes": 1024,
                })
        self.assertTrue(guard["disk_low"])


if __name__ == "__main__":
    unittest.main()
