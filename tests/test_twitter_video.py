"""Twitter Matroska uploads must become seekable MP4s before reaching Telegram."""

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, patch

from bot import downloader
from bot.handlers import download as handler
from bot.media_parts import MediaPreparationError, _streaming_mp4


class TwitterVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_remuxes_source_and_uses_file_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mkv"
            source.write_bytes(b"source")
            output = Path(directory) / "telegram-source.mp4"
            output.write_bytes(b"normalized")
            with (
                patch.object(downloader, "_download_best", AsyncMock(
                    return_value=(source, {"title": "video", "duration": None}),
                )),
                patch.object(downloader, "prepare_streaming_mp4", AsyncMock(
                    return_value=output,
                )) as prepare,
                patch.object(downloader, "_ffprobe_duration", return_value=3),
                patch.object(downloader, "_ffprobe_dimensions", return_value=(1080, 1920)),
            ):
                async with downloader._temporary_download(
                    "https://x.com/example/status/123", {}, "best", "twitter-test-",
                    100, None, {}, False,
                ) as media:
                    self.assertEqual(media.path, output)
                    self.assertEqual(media.duration, 3)
                    self.assertEqual(media.file_size, len(b"normalized"))
            prepare.assert_awaited_once_with(source, None)

    async def test_selected_quality_cache_does_not_reuse_old_twitter_file_id(self):
        with (
            patch.object(handler, "_safe_edit", AsyncMock()),
            patch.object(handler, "probe_quality_options", AsyncMock(
                return_value=[downloader.QualityOption(480, None)],
            )),
            patch.object(handler, "_MEDIA_CHOICE_TIMEOUT", 0.001),
        ):
            from types import SimpleNamespace
            choice = await handler._select_quality(
                object(), "twitter-cache-test", "https://x.com/example/status/123",
                SimpleNamespace(cookies_file=None), 100,
            )
        key = handler._video_cache_key("twitter", height=choice.height)
        self.assertNotIn(key, {"video", "video:480", "video_capped"})
        self.assertEqual(handler._video_cache_key("youtube", height=480), "video:480")
        self.assertEqual(handler._video_cache_key("tiktok", capped=True),
                         "video_hdr_metadata_v1_capped")

    def test_cancelled_remux_never_starts_ffmpeg(self):
        cancel = Event()
        cancel.set()
        with patch("bot.media_parts._run_ffmpeg") as run, self.assertRaises(MediaPreparationError):
            _streaming_mp4(Path("source.mkv"), cancel)
        run.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg/ffprobe not installed locally")
    def test_real_mkv_remux_preserves_packets_geometry_and_seeking(self):
        def packets(path):
            result = subprocess.run([
                "ffprobe", "-v", "error", "-show_packets", "-show_data_hash", "sha256",
                "-show_entries", "packet=stream_index,data_hash", "-of", "json", str(path),
            ], capture_output=True, text=True, check=True, timeout=30)
            streams = {}
            for packet in json.loads(result.stdout)["packets"]:
                streams.setdefault(packet["stream_index"], []).append(packet["data_hash"])
            return streams

        with tempfile.TemporaryDirectory() as directory:
            for index, (geometry, audio) in enumerate((("320x240", True),
                                                      ("240x320", True), ("320x240", False))):
                with self.subTest(geometry=geometry, audio=audio):
                    source = Path(directory) / f"source-{index}.mkv"
                    command = ["ffmpeg", "-nostdin", "-v", "error", "-y",
                               "-f", "lavfi", "-i", f"testsrc2=size={geometry}:duration=2:rate=30"]
                    if audio:
                        command += ["-f", "lavfi", "-i", "sine=duration=2", "-c:a", "aac"]
                    command += ["-c:v", "libx264", "-threads", "2", str(source)]
                    subprocess.run(command, check=True, timeout=30)
                    output = _streaming_mp4(source, None)
                    self.assertEqual(packets(source), packets(output))
                    probe = subprocess.run([
                        "ffprobe", "-v", "error", "-show_entries",
                        "stream=width,height:format=format_name,duration", "-of", "json",
                        str(output),
                    ], capture_output=True, text=True, check=True, timeout=30)
                    info = json.loads(probe.stdout)
                    self.assertIn("mp4", info["format"]["format_name"])
                    self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=.15)
                    self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]),
                                     tuple(map(int, geometry.split("x"))))
                    atoms = []
                    contents = output.read_bytes()
                    offset = 0
                    while offset < len(contents):
                        length, kind = struct.unpack_from(">I4s", contents, offset)
                        self.assertGreaterEqual(length, 8)
                        atoms.append(kind)
                        offset += length
                    self.assertLess(atoms.index(b"moov"), atoms.index(b"mdat"))
                    subprocess.run([
                        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-ss", "1",
                        "-i", str(output), "-f", "null", "-",
                    ], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
