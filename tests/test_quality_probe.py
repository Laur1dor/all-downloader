"""A slow metadata endpoint must not hold the user's quality prompt open."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from yt_dlp.utils import DownloadError

from bot import downloader, rule34video


class QualityProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_source_menu_sizes_are_estimates_with_explicit_unknowns(self) -> None:
        page_url = "https://rule34video.com/video/1/"
        labels = ((1080, "28 MB", 28_000_000), (720, "1,5 GB", 1_500_000_000),
                  (480, "800 KiB", 819_200), (360, "unknown", None), (240, "0 MB", None))
        page = ''.join(
            f'<a href="/get_file/1/{height}.mp4/?download=true">MP4 {height}p'
            f'<span class="vp-sheet__meta">{label}</span></a>'
            for height, label, _size in labels
        )
        with patch.object(rule34video, "ensure_public_url"):
            formats = rule34video.download_formats(page, page_url)
        self.assertEqual([fmt["filesize_approx"] for fmt in formats],
                         [size for _height, _label, size in labels])
        with (
            patch.object(downloader, "_extract_info", return_value={"formats": formats}),
            patch.object(downloader, "_remote_size") as head,
        ):
            options = downloader._probe_quality_options_sync(page_url, {})
        head.assert_not_called()
        self.assertIn("~27 МБ", options[0].label)
        self.assertIn("<1 МБ", options[2].label)
        self.assertIn("размер неизвестен", options[3].label)
        self.assertEqual([option.height for option in options], [1080, 720, 480, 360, 240])

    def test_slow_size_probe_keeps_source_heights_without_peak_hls_size(self) -> None:
        release = threading.Event()
        info = {"duration": 10, "formats": [
            {"height": 720, "format_id": "hls", "protocol": "m3u8_native",
             "tbr": 800, "url": "https://media.example/720.m3u8"},
            {"height": 480, "format_id": "http", "url": "https://media.example/480.mp4"},
        ]}

        def slow_size(*_args, **_kwargs):
            release.wait(2)
            return 5_000_000

        with (
            patch.object(downloader, "_extract_info", return_value=info),
            patch.object(downloader, "_hls_size", side_effect=slow_size),
            patch.object(downloader, "_remote_size", side_effect=slow_size),
            patch.object(downloader, "_QUALITY_SIZE_PROBE_SECONDS", 0.02),
        ):
            try:
                options = downloader._probe_quality_options_sync("https://youtu.be/test", {})
                self.assertEqual([option.height for option in options], [720, 480])
                self.assertIsNone(options[0].estimated_size)
                self.assertIsNone(options[1].estimated_size)
            finally:
                release.set()

    def test_missing_hls_measurement_does_not_reject_small_stream_by_peak(self) -> None:
        info = {"duration": 1200, "formats": [
            {"height": 360, "format_id": "hls", "protocol": "m3u8_native",
             "tbr": 2560, "filesize_approx": 400_000_000,
             "url": "https://media.example/360.m3u8", "acodec": "aac"},
        ]}
        with (
            patch.object(downloader, "_extract_info", return_value=info),
            patch.object(downloader, "_hls_size", return_value=None),
        ):
            options = downloader._probe_quality_options_sync("https://youtu.be/test", {})
        self.assertEqual(options[0].height, 360)
        self.assertIsNone(options[0].estimated_size)
        self.assertIn("размер неизвестен", options[0].label)

    def test_hls_measurement_and_explicit_exact_size_remain_available(self) -> None:
        info = {"duration": 1200, "formats": [
            {"height": 360, "format_id": "hls", "protocol": "m3u8_native",
             "filesize": 45_000_000, "filesize_approx": 400_000_000,
             "url": "https://media.example/360.m3u8", "acodec": "aac"},
        ]}
        for measured, expected in ((None, 45_000_000), (35_000_000, 35_000_000)):
            with self.subTest(measured=measured), patch.object(
                downloader, "_extract_info", return_value=info,
            ), patch.object(downloader, "_hls_size", return_value=measured):
                options = downloader._probe_quality_options_sync("https://youtu.be/test", {})
            self.assertEqual(options[0].estimated_size, expected)

    def test_optional_size_failure_keeps_all_heights(self) -> None:
        info = {"formats": [
            {"height": 480, "format_id": "http", "url": "https://media.example/480.mp4"},
        ]}
        with (
            patch.object(downloader, "_extract_info", return_value=info),
            patch.object(downloader, "_remote_size", side_effect=OSError("CDN unavailable")),
        ):
            options = downloader._probe_quality_options_sync("https://youtu.be/test", {})
        self.assertEqual(options[0].height, 480)
        self.assertIsNone(options[0].estimated_size)

    def test_rule34video_lists_every_source_height_without_cdn_size_requests(self) -> None:
        info = {
            "formats": [
                {"height": height, "format_id": f"mp4-{height}",
                 "url": f"https://rule34video.com/get_file/1/{height}.mp4"}
                for height in (360, 480, 720, 1080)
            ],
        }
        with (
            patch.object(downloader, "_extract_info", return_value=info),
            patch.object(downloader, "_remote_size") as head,
        ):
            options = downloader._probe_quality_options_sync(
                "https://rule34video.com/video/1/", {},
            )
        head.assert_not_called()
        self.assertEqual([option.height for option in options], [1080, 720, 480, 360])
        self.assertTrue(all(option.estimated_size is None for option in options))
        self.assertEqual(options[0].format_id, "mp4-1080")

    def test_rule34video_transport_probe_tries_distinct_exit(self) -> None:
        info = {"formats": [{"height": 720, "format_id": "mp4-720"}]}
        with (
            patch.object(
                downloader, "_extract_info",
                side_effect=[DownloadError("Read timed out"), info],
            ) as extract,
            patch.object(downloader, "forced_proxy", return_value="socks5h://xray:2077"),
        ):
            options = downloader._probe_quality_options_sync(
                "https://rule34video.com/video/1/", {"proxy": "socks5h://warp:1080"},
            )
        self.assertEqual(options[0].height, 720)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(extract.call_args_list[0].kwargs["attempts"], 1)
        self.assertEqual(extract.call_args_list[1].args[0]["proxy"], "socks5h://xray:2077")

    async def test_typed_metadata_failure_falls_back_without_crashing(self) -> None:
        with (
            patch.object(downloader, "_limited_quality_probe",
                         side_effect=downloader.DownloadFailedError("Source unavailable")),
            patch.object(downloader, "_base_options", return_value={}),
            patch.object(downloader, "proxy_for", return_value=None),
        ):
            self.assertEqual(
                await downloader.probe_quality_options("https://rule34video.com/video/1/", None),
                [],
            )

    def test_youtube_prefers_direct_h264_and_matching_aac_size(self) -> None:
        info = {
            "duration": 10529,
            "formats": [
                {"format_id": "311", "height": 720, "vcodec": "avc1.4D4020",
                 "acodec": "none", "protocol": "m3u8_native", "fps": 60,
                 "filesize_approx": 6_380_000_000},
                {"format_id": "298", "height": 720, "vcodec": "avc1.4d4020",
                 "acodec": "none", "protocol": "https", "fps": 60,
                 "filesize": 4_340_000_000},
                {"format_id": "251", "vcodec": "none", "acodec": "opus",
                 "ext": "webm", "filesize": 500_000_000},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2",
                 "ext": "m4a", "filesize": 170_000_000},
            ],
        }
        with patch.object(downloader, "_extract_info", return_value=info):
            options = downloader._probe_quality_options_sync(
                "https://www.youtube.com/watch?v=aSRjM7HGH0g", {},
            )
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].format_id, "298")
        self.assertEqual(options[0].estimated_size, 4_510_000_000)
        self.assertTrue(downloader.quality_selector(options[0]).startswith(
            "298+ba[ext=m4a]/298+ba/",
        ))

    async def test_timeout_falls_back_and_limits_abandoned_workers(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_probe(_url, _options, _proxy):
            nonlocal calls
            calls += 1
            started.set()
            release.wait(2)
            return []

        with patch.object(downloader, "_probe_quality_options_sync", slow_probe), \
             patch.object(downloader, "_QUALITY_PROBE_TIMEOUT", 0.02), \
             patch.object(downloader, "_base_options", return_value={}), \
             patch.object(downloader, "proxy_for", return_value=None):
            try:
                self.assertEqual(
                    await downloader.probe_quality_options("https://youtu.be/test", None),
                    [],
                )
                self.assertTrue(started.is_set())
                self.assertEqual(
                    await downloader.probe_quality_options("https://youtu.be/next", None),
                    [],
                )
                self.assertEqual(calls, 1)
            finally:
                release.set()
                await asyncio.sleep(0.05)
