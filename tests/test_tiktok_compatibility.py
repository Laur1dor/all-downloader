"""Unsupported TikTok codecs need an SDR preview and an intact original file."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from bot import downloader
from bot.media_parts import MediaPreparationError
from bot.progress import PHASE_PREPARE, ProgressState


def _probe(transfer: str | None = None):
    video = {"codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le"}
    if transfer:
        video["color_transfer"] = transfer
    return MagicMock(returncode=0, stdout=json.dumps({
        "streams": [video], "format": {"duration": "2"},
    }))


class TikTokCompatibilityTests(unittest.TestCase):
    def test_playable_document_requires_duration_and_dimensions(self) -> None:
        for duration, width, height, codec in (
            ("0", 960, 1920, "h264"),
            ("32", 0, 1920, "h264"),
            ("32", 960, 0, "h264"),
            ("32", 960, 1920, "hevc"),
        ):
            with self.subTest(duration=duration, width=width, height=height, codec=codec):
                result = MagicMock(returncode=0, stdout=json.dumps({
                    "format": {"duration": duration, "format_name": "mov,mp4"},
                    "streams": [{"codec_type": "video", "codec_name": codec,
                                 "pix_fmt": "yuv420p", "width": width, "height": height}],
                }))
                with patch.object(downloader.subprocess, "run", return_value=result) as run:
                    self.assertFalse(downloader._tiktok_playable(Path("document.mp4")))
                run.assert_called_once()

    def test_original_only_cold_chain_fetches_hdr_bytes_without_preview_work(self) -> None:
        url = "https://vt.tiktok.com/example/"
        session = MagicMock(proxies={})
        guard = {}

        def fetch(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"raw HDR source")
            guard["_last_complete_body"] = True
            return True

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(downloader, "_tiktok_item", side_effect=downloader.DownloadFailedError(
                 "page unavailable",
             )), patch.object(downloader, "_tikwm_data", return_value=({
                 "id": "123", "play": "https://v19.tiktokcdn-us.com/video", "duration": 32,
             }, session)), patch.object(downloader, "_stream_to_file", side_effect=fetch), \
             patch.object(downloader, "_tiktok_original_valid", return_value=True), \
             patch.object(downloader, "_tiktok_playable") as playable, \
             patch.object(downloader, "_transcode_tiktok_compatible") as encode:
            result, info = downloader._download_tiktok_sync(
                url, directory, None, 100_000, None, None, guard, original_only=True,
            )
            self.assertEqual(result.read_bytes(), b"raw HDR source")
            self.assertIsNone(info["original_path"])
            playable.assert_not_called()
            encode.assert_not_called()
        session.close.assert_called_once()

        marker = object()
        with patch.object(downloader, "_tiktok_download", return_value=marker) as source:
            self.assertIs(downloader.download_video(url, None, original_only=True), marker)
        self.assertTrue(source.call_args.kwargs["original_only"])

    def test_original_only_requires_complete_body_and_valid_media_structure(self) -> None:
        probe = MagicMock(returncode=0, stdout=json.dumps({
            "streams": [{"codec_type": "video", "codec_name": "hevc"}],
            "format": {"duration": "32", "format_name": "mov,mp4"},
        }))
        with patch.object(downloader.subprocess, "run", return_value=probe):
            self.assertTrue(downloader._tiktok_original_valid(Path("original.mp4")))
        with patch.object(downloader.subprocess, "run", return_value=MagicMock(
            returncode=0, stdout='{"streams":[],"format":{"duration":"32"}}',
        )):
            self.assertFalse(downloader._tiktok_original_valid(Path("invalid.mp4")))

        def fetch(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"unconfirmed partial HDR")
            return True

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(downloader, "_stream_to_file", side_effect=fetch), \
             patch.object(downloader, "_tiktok_original_valid", return_value=True) as structural, \
             self.assertRaises(downloader.ConfirmedVideoError):
            downloader._fetch_tiktok_media(
                {"id": "123", "video": {"playAddr": "https://v19.tiktokcdn-us.com/video"}},
                MagicMock(proxies={}), directory, None, 100_000, None, None, {},
                resolver=True, original_only=True,
            )
        structural.assert_not_called()

    def test_hdr_conversion_tonemaps_and_preserves_geometry(self) -> None:
        for transfer in ("arib-std-b67", "smpte2084", None):
            with self.subTest(transfer=transfer), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"original HDR")

                def encode(command, timeout, _cancel):
                    self.assertEqual(timeout, 120)
                    Path(command[-1]).write_bytes(b"compatible")

                with patch.object(downloader.subprocess, "run", side_effect=[
                    _probe(transfer),
                    MagicMock(returncode=0, stdout='{"format":{"duration":"2"}}'),
                ]), patch.object(downloader, "_run_ffmpeg", side_effect=encode) as run:
                    result = downloader._transcode_tiktok_compatible(source, 10_000, None)
                self.assertIsNotNone(result)
                self.assertEqual(source.read_bytes(), b"original HDR")
                command = run.call_args.args[0]
                filters = command[command.index("-vf") + 1]
                self.assertEqual("tonemap=" in filters, transfer is not None)
                self.assertIn("trunc(iw/2)*2", filters)
                self.assertNotIn("1080", filters)
                self.assertEqual(command[command.index("-c:v") + 1], "libx264")
                self.assertEqual(command[command.index("-color_trc") + 1], "bt709")

    def test_invalid_and_truncated_sources_are_not_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"bad source")
            with patch.object(downloader.subprocess, "run", return_value=MagicMock(
                returncode=1, stdout="{}",
            )), patch.object(downloader, "_run_ffmpeg") as encode:
                self.assertIsNone(downloader._transcode_tiktok_compatible(source, 10_000, None))
            encode.assert_not_called()

            def encode(command, *_args):
                Path(command[-1]).write_bytes(b"truncated")

            with patch.object(downloader.subprocess, "run", side_effect=[
                _probe(), MagicMock(returncode=0, stdout='{"format":{"duration":"0.2"}}'),
            ]), patch.object(downloader, "_run_ffmpeg", side_effect=encode):
                self.assertIsNone(downloader._transcode_tiktok_compatible(source, 10_000, None))
            self.assertFalse(source.with_name("source-telegram.mp4").exists())
            self.assertEqual(source.read_bytes(), b"bad source")

    def test_cancellation_removes_partial_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"source")
            cancel = Event()

            def interrupted(command, *_args):
                Path(command[-1]).write_bytes(b"partial")
                cancel.set()
                raise MediaPreparationError("cancelled")

            with patch.object(downloader.subprocess, "run", return_value=_probe()), \
                 patch.object(downloader, "_run_ffmpeg", side_effect=interrupted), \
                 self.assertRaises(downloader.DownloadCancelledError):
                downloader._transcode_tiktok_compatible(source, 10_000, cancel)
            self.assertFalse(source.with_name("source-telegram.mp4").exists())
            self.assertTrue(source.exists())

    def test_primary_post_audio_is_checked_and_auxiliary_tracks_are_not_encoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original with two audio tracks")
            initial = json.loads(_probe("arib-std-b67").stdout)
            initial["streams"].extend([
                {"codec_type": "audio", "codec_name": "aac"},
                {"codec_type": "audio", "codec_name": "aac"},
            ])

            def encode(command, *_args):
                if command[-1] != "-":
                    Path(command[-1]).write_bytes(b"compatible")

            with patch.object(downloader.subprocess, "run", side_effect=[
                MagicMock(returncode=0, stdout=json.dumps(initial)),
                MagicMock(returncode=0, stdout='{"format":{"duration":"2"}}'),
            ]), patch.object(downloader, "_run_ffmpeg", side_effect=encode) as run:
                self.assertIsNotNone(downloader._transcode_tiktok_compatible(source, 10_000, None))
            self.assertEqual(run.call_count, 2)
            self.assertIn("0:a:0", run.call_args_list[0].args[0])
            self.assertIn("-xerror", run.call_args_list[0].args[0])
            self.assertIn("0:a:0?", run.call_args_list[1].args[0])
            self.assertNotIn("0:a?", run.call_args_list[1].args[0])

            with patch.object(downloader.subprocess, "run", return_value=MagicMock(
                returncode=0, stdout=json.dumps(initial),
            )), patch.object(downloader, "_run_ffmpeg", side_effect=MediaPreparationError(
                "primary audio invalid",
            )) as run:
                self.assertIsNone(downloader._transcode_tiktok_compatible(source, 10_000, None))
            run.assert_called_once()
            self.assertTrue(source.exists())

    def test_fetch_keeps_raw_original_for_the_file_button(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = MagicMock(proxies={})
            item = {"id": "123", "video": {"playAddr": "https://v19.tiktokcdn-us.com/video"}}
            progress = ProgressState()
            previous_start = progress.started_at

            def fetch(_session, _url, destination, *_args, **_kwargs):
                destination.write_bytes(b"original HDR")
                return True

            def convert(source, *_args):
                self.assertEqual(progress.phase, PHASE_PREPARE)
                self.assertGreaterEqual(progress.started_at, previous_start)
                result = source.with_name("123-telegram.mp4")
                result.write_bytes(b"compatible")
                return result

            with patch.object(downloader, "_stream_to_file", side_effect=fetch), \
                 patch.object(downloader, "_tiktok_playable", side_effect=[False, True]), \
                 patch.object(downloader, "_repair_tiktok_sei", return_value=None), \
                 patch.object(downloader, "_transcode_tiktok_compatible", side_effect=convert):
                result, info = downloader._fetch_tiktok_media(
                    item, session, directory, None, 10_000, progress, None, {}, resolver=True,
                )
            self.assertEqual(result.read_bytes(), b"compatible")
            self.assertEqual(info["original_path"].read_bytes(), b"original HDR")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg/ffprobe not installed locally")
    def test_real_hevc_hdr_converts_to_sdr_without_resize_or_audio_requirement(self) -> None:
        for transfer in ("arib-std-b67", "smpte2084", "bt709"):
            with self.subTest(transfer=transfer), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                hdr = transfer != "bt709"
                subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "1",
                    "-f", "lavfi", "-i", "testsrc2=size=96x192:rate=15:duration=1",
                    "-c:v", "libx265", "-x265-params", "pools=1:frame-threads=1:log-level=error",
                    "-pix_fmt", "yuv420p10le", "-color_trc", transfer,
                    "-color_primaries", "bt2020" if hdr else "bt709",
                    "-colorspace", "bt2020nc" if hdr else "bt709", str(source),
                ], capture_output=True, check=True, timeout=30)
                result = downloader._transcode_tiktok_compatible(source, 5_000_000, None)
                self.assertIsNotNone(result)
                self.assertTrue(downloader._tiktok_playable(result))
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_name,pix_fmt,width,height,color_transfer,color_primaries",
                    "-of", "json", str(result),
                ], capture_output=True, text=True, check=True, timeout=30)
                video = json.loads(probe.stdout)["streams"][0]
                self.assertEqual((video["width"], video["height"]), (96, 192))
                self.assertEqual(video["codec_name"], "h264")
                self.assertEqual(video["pix_fmt"], "yuv420p")
                self.assertEqual(video["color_transfer"], "bt709")
                self.assertEqual(video["color_primaries"], "bt709")
                decoded = subprocess.run([
                    "ffmpeg", "-nostdin", "-v", "error", "-i", str(result),
                    "-frames:v", "1", "-vf", "scale=16:16,format=gray",
                    "-f", "rawvideo", "pipe:1",
                ], capture_output=True, check=True, timeout=30)
                self.assertEqual(len(decoded.stdout), 256)
                self.assertGreater(max(decoded.stdout) - min(decoded.stdout), 20)
