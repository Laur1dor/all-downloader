"""Focused tests for bounded trimming and part preparation."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from bot.media_parts import (
    MediaPreparationError,
    _prepare,
    _trim_audio,
    parse_time_range,
)


class MediaPartsTests(unittest.TestCase):
    def test_time_range_rejects_invalid_or_unbounded_intervals(self) -> None:
        self.assertEqual(parse_time_range("01:20-02:40"), (80, 160))
        self.assertEqual(parse_time_range("01:00:00-01:02:00"), (3600, 3720))
        for value in ("01:20 - 02:40", "01:20–02:40", "01:20 — 02:40"):
            with self.subTest(value=value):
                self.assertEqual(parse_time_range(value), (80, 160))
        self.assertIsNone(parse_time_range("random text"))
        for value in ("00:60-01:01", "02:00-01:00", "00:00-05:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time_range(value)

    def test_splits_with_a_hard_part_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"x" * 1_000_000)
            calls: list[tuple[float, float]] = []

            def fake_cut(_source: Path, output: Path, start: float,
                         length: float, _timeout: float, _bitrate: int,
                         _cancel: Event | None) -> None:
                calls.append((start, length))
                output.write_bytes(b"x" * int(1_000_000 * length / 4))

            with patch("bot.media_parts._duration", return_value=4), \
                 patch("bot.media_parts._cut", side_effect=fake_cut), \
                 patch("bot.media_parts.shutil.disk_usage") as usage:
                usage.return_value.free = 10 * 1024**3
                parts = _prepare(source, 400_000, None, None)
            self.assertEqual(len(parts), 4)
            self.assertTrue(all(part.stat().st_size <= 400_000 for part in parts))
            self.assertEqual(sum(length for _, length in calls), 4)

    def test_sparse_source_over_two_gib_is_split_within_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "large.mp4"
            source.write_bytes(b"source")
            real_stat = Path.stat

            def fake_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == source:
                    class LargeStat:
                        st_size = 2200 * 1024**2
                    return LargeStat()
                return result

            def fake_segment(_source, directory, _seconds, _timeout, _cancel):
                paths = [directory / f"part-{number:02d}.mp4" for number in (1, 2)]
                for path in paths:
                    path.write_bytes(b"part")
                return paths

            with patch("bot.media_parts._duration", return_value=600), \
                 patch("bot.media_parts._source_compatible", return_value=True), \
                 patch("bot.media_parts._segment_copy", side_effect=fake_segment) as segment, \
                 patch.object(Path, "stat", fake_stat), \
                 patch("bot.media_parts.shutil.disk_usage") as usage:
                usage.return_value.free = 10 * 1024**3
                parts = _prepare(source, 2000 * 1024**2, None, None)
            self.assertEqual(len(parts), 2)
            segment.assert_called_once()

    def test_six_gib_source_is_transcoded_into_at_most_eight_safe_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "large.mp4"
            source.write_bytes(b"source")
            real_stat = Path.stat

            def fake_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if path == source:
                    class LargeStat:
                        st_size = 6 * 1024**3
                    return LargeStat()
                return result

            def fake_cut(_source, output, *_args):
                output.write_bytes(b"part")

            with patch("bot.media_parts._duration", return_value=4 * 3600), \
                 patch("bot.media_parts._source_compatible", return_value=False), \
                 patch("bot.media_parts._cut", side_effect=fake_cut) as cut, \
                 patch.object(Path, "stat", fake_stat), \
                 patch("bot.media_parts.shutil.disk_usage") as usage:
                usage.return_value.free = 14 * 1024**3
                parts = _prepare(source, 768 * 1024**2, None, None)
            self.assertEqual(len(parts), 8)
            self.assertEqual(cut.call_count, 8)

    def test_cancelled_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"x" * 1000)
            cancelled = Event()
            cancelled.set()
            with patch("bot.media_parts._duration", return_value=100), \
                 patch("bot.media_parts.shutil.disk_usage") as usage:
                usage.return_value.free = 10 * 1024**3
                with self.assertRaises(MediaPreparationError):
                    _prepare(source, 400, None, cancelled)

    def test_audio_trim_uses_requested_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp3"
            source.write_bytes(b"source")

            def fake_ffmpeg(command, _timeout, _cancel):
                Path(command[-1]).write_bytes(b"audio")

            with patch("bot.media_parts._duration", side_effect=[100.0, 20.0]), \
                 patch("bot.media_parts._run_ffmpeg", side_effect=fake_ffmpeg) as run:
                result = _trim_audio(source, (10, 30), None)
            self.assertEqual(result.name, "trimmed.m4a")
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("-ss") + 1], "10")
            self.assertEqual(args[args.index("-t") + 1], "20")

    def test_clip_is_transcoded_even_when_under_file_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.write_bytes(b"x" * 1000)

            def fake_cut(_source: Path, output: Path, *_args) -> None:
                output.write_bytes(b"converted")

            with patch("bot.media_parts._duration", return_value=1), \
                 patch("bot.media_parts._cut", side_effect=fake_cut) as cut, \
                 patch("bot.media_parts.shutil.disk_usage") as usage:
                usage.return_value.free = 10 * 1024**3
                result = _prepare(source, 1_000_000, None, None, True)
            self.assertEqual(len(result), 1)
            self.assertNotEqual(result[0], source)
            cut.assert_called_once()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg/ffprobe not installed locally")
    def test_real_clip_has_compatible_codecs_and_keyframe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=4",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                "-c:v", "mpeg4", "-c:a", "mp3", str(source),
            ], check=True, timeout=30)
            result = _prepare(source, 5_000_000, (1, 3), None)
            self.assertEqual(len(result), 1)
            probe = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", (
                    "stream=codec_name,pix_fmt,codec_type:"
                    "format=duration:frame=key_frame"
                ),
                "-of", "json", str(result[0]),
            ], capture_output=True, text=True, check=True, timeout=30)
            import json
            info = json.loads(probe.stdout)
            self.assertEqual(info["streams"][0]["codec_name"], "h264")
            self.assertEqual(info["streams"][0]["pix_fmt"], "yuv420p")
            self.assertEqual(info["streams"][1]["codec_name"], "aac")
            self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=0.2)
            self.assertEqual(info["frames"][0]["key_frame"], 1)
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i",
                str(result[0]), "-f", "null", "-",
            ], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
