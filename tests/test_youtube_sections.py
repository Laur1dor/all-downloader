"""YouTube excerpts must bypass whole-source size estimates."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from bot import downloader


class YouTubeSectionTests(unittest.TestCase):
    def test_download_sets_ranged_request_and_forced_keyframes(self) -> None:
        marker = object()
        with patch.dict("os.environ", {"BOT_RANGED_WORKER": "1"}), \
             patch.object(downloader, "_temporary_download", return_value=marker) as temporary:
            result = downloader.download_video(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                None,
                time_range=(10, 20),
            )
        self.assertIs(result, marker)
        options = temporary.call_args.args[1]
        self.assertTrue(options["force_keyframes_at_cuts"])
        self.assertEqual(
            options["external_downloader_args"],
            {"ffmpeg_i": ["-rw_timeout", "30000000"]},
        )
        self.assertEqual(options["socket_timeout"], 15)
        self.assertEqual(options["retries"], 1)
        self.assertEqual(options["fragment_retries"], 1)
        self.assertEqual(
            list(options["download_ranges"]({"duration": 213}, None)),
            [{"start_time": 10, "end_time": 20}],
        )


class YouTubeSectionSizeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _pending_process():
        class Input:
            def write(self, _data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        class Process:
            returncode = None
            pid = 1
            stdin = Input()

            async def wait(self):
                self.returncode = 1
                return 1

        return Process()

    async def test_ranged_download_ignores_full_source_size(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "excerpt.mp4"
            path.write_bytes(b"excerpt")
            with patch.object(downloader, "_probe_filesize", return_value=7 * 1024**3) as probe, \
                 patch.object(downloader, "_download_sync", return_value=(path, {})):
                actual, _ = await downloader._download_best(
                    "https://youtu.be/example",
                    {"download_ranges": object()},
                    50 * 1024**2,
                    {},
                )
            self.assertEqual(actual, path)
            probe.assert_not_called()

    async def test_ranged_supervisor_enforces_temp_size_limit(self) -> None:
        process = self._pending_process()
        with patch.object(downloader.asyncio, "create_subprocess_exec", return_value=process), \
             patch.object(downloader, "_ranged_temp_bytes", return_value=101), \
             patch.object(downloader, "_stop_ranged_process") as stop, \
             self.assertRaises(downloader.OversizedError):
            async with downloader._ranged_download(
                "https://youtu.be/example", None, 100, None, None,
                False, None, False, None, (10, 20),
            ):
                self.fail("oversized temporary download was delivered")
        stop.assert_awaited_once_with(process)

    async def test_ranged_supervisor_honours_cancellation(self) -> None:
        process = self._pending_process()
        cancelled = Event()
        cancelled.set()
        with patch.object(downloader.asyncio, "create_subprocess_exec", return_value=process), \
             patch.object(downloader, "_stop_ranged_process") as stop, \
             self.assertRaises(downloader.DownloadCancelledError):
            async with downloader._ranged_download(
                "https://youtu.be/example", None, 100, None, cancelled,
                False, None, False, None, (10, 20),
            ):
                self.fail("cancelled temporary download was delivered")
        stop.assert_awaited_once_with(process)

    async def test_ranged_supervisor_enforces_wall_deadline(self) -> None:
        process = self._pending_process()
        with patch.object(downloader.asyncio, "create_subprocess_exec", return_value=process), \
             patch.object(downloader, "_RANGED_WORK_SECONDS", -1), \
             patch.object(downloader, "_stop_ranged_process") as stop, \
             self.assertRaisesRegex(downloader.DownloadFailedError, "30 минут"):
            async with downloader._ranged_download(
                "https://youtu.be/example", None, 100, None, None,
                False, None, False, None, (10, 20),
            ):
                self.fail("expired temporary download was delivered")
        stop.assert_awaited_once_with(process)


if __name__ == "__main__":
    unittest.main()
