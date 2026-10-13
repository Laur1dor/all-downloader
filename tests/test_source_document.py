"""Source documents preserve HDR/video packets while repairing MP4 packaging."""

from __future__ import annotations

import hashlib
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


class SourceDocumentTests(unittest.TestCase):
    def test_hdr_flag_requires_pq_or_hlg_instead_of_bit_depth_alone(self) -> None:
        for transfer, expected in (("arib-std-b67", True), ("smpte2084", True), ("bt709", False)):
            with self.subTest(transfer=transfer), patch.object(downloader.subprocess, "run",
                return_value=MagicMock(returncode=0, stdout=json.dumps({"streams": [{
                    "color_transfer": transfer, "pix_fmt": "yuv420p10le",
                }]})),
            ):
                self.assertEqual(downloader._source_hdr(Path("source.mp4")), expected)

    def test_healthy_faststart_source_is_returned_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"unaltered source")
            video = {"width": 960, "height": 1920, "color_transfer": "arib-std-b67"}
            with patch.object(downloader, "_source_document_probe",
                              return_value=({}, video, 32.5)), \
                 patch.object(downloader, "_source_document_decode"), \
                 patch.object(downloader, "_source_document_faststart", return_value=True), \
                 patch.object(downloader, "_run_ffmpeg") as remux:
                result = downloader._prepare_source_document(source, 100_000, None)
            self.assertEqual(result.path, source)
            self.assertTrue(result.source_hdr)
            self.assertFalse(result.structure_modified)
            self.assertEqual(source.read_bytes(), b"unaltered source")
            remux.assert_not_called()

    def test_bad_auxiliary_audio_is_dropped_without_video_or_primary_audio_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"HDR with bad auxiliary AAC")
            video = {"width": 960, "height": 1920, "color_transfer": "arib-std-b67",
                     "codec_name": "hevc", "pix_fmt": "yuv420p10le",
                     "color_primaries": "bt2020", "color_space": "bt2020nc"}

            def remux(command, *_args):
                Path(command[-1]).write_bytes(b"remuxed HDR")

            with patch.object(downloader, "_source_document_probe",
                              return_value=({"streams": [{"codec_type": "audio"}] * 3},
                                            video, 32.5)), \
                 patch.object(downloader, "_source_document_audio_decode", side_effect=[
                     None, MediaPreparationError("bad extra audio"), None,
                 ]) as audio_decode, \
                 patch.object(downloader, "_source_document_decode") as decode, \
                 patch.object(downloader, "_run_ffmpeg", side_effect=remux) as run, \
                 patch.object(downloader, "_source_video_packet_hash", return_value="same"), \
                 patch.object(downloader, "_source_document_faststart", return_value=True), \
                 patch.object(downloader.shutil, "disk_usage") as usage:
                usage.return_value.free = 10 * 1024**3
                result = downloader._prepare_source_document(source, 100_000, None)
            self.assertTrue(result.source_hdr)
            self.assertTrue(result.structure_modified)
            self.assertEqual([call.args[1] for call in audio_decode.call_args_list], [0, 1, 2])
            decode.assert_called_once_with(result.path, None)
            command = run.call_args.args[0]
            self.assertIn("-0:a:1", command)
            self.assertNotIn("-0:a:2", command)
            self.assertEqual(command[command.index("-c") + 1], "copy")
            self.assertNotIn("-vf", command)
            self.assertEqual(command[command.index("-tag:v") + 1], "hvc1")
            self.assertEqual(source.read_bytes(), b"HDR with bad auxiliary AAC")

    def test_broken_primary_audio_is_rejected_without_silent_sound_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"broken source")
            video = {"width": 960, "height": 1920, "color_transfer": "arib-std-b67"}
            with patch.object(downloader, "_source_document_probe",
                              return_value=({"streams": [{"codec_type": "audio"}]},
                                            video, 32.5)), \
                 patch.object(downloader, "_source_document_audio_decode",
                              side_effect=MediaPreparationError("primary audio broken")), \
                 patch.object(downloader, "_run_ffmpeg") as remux, \
                 self.assertRaises(downloader.DownloadFailedError):
                downloader._prepare_source_document(source, 100_000, None)
            remux.assert_not_called()
            self.assertFalse(source.with_name("source-source.mp4").exists())

    def test_cancelled_source_does_not_start_probe_or_remux(self) -> None:
        cancel = Event()
        cancel.set()
        with patch.object(downloader, "_source_document_probe") as probe, \
             self.assertRaises(downloader.DownloadCancelledError):
            downloader._prepare_source_document(Path("unused.mp4"), 100_000, cancel)
        probe.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "ffmpeg/ffprobe not installed locally")
    def test_real_hdr_source_packets_metadata_and_primary_audio_are_preserved(self) -> None:
        for transfer, faststart in (("arib-std-b67", True), ("smpte2084", False), ("bt709", False)):
            with self.subTest(transfer=transfer), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                hdr = transfer != "bt709"
                transfer_id = {"arib-std-b67": 18, "smpte2084": 16, "bt709": 1}[transfer]
                color_id = 9 if hdr else 1
                command = [
                    "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "1",
                    "-f", "lavfi", "-i", "testsrc2=size=96x192:rate=15:duration=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:v", "libx265", "-x265-params", (
                        "pools=1:frame-threads=1:log-level=error:"
                        f"colorprim={color_id}:transfer={transfer_id}:colormatrix={color_id}"
                    ),
                    "-pix_fmt:v", "yuv420p10le", "-color_trc:v", transfer,
                    "-color_primaries:v", "bt2020" if hdr else "bt709",
                    "-colorspace:v", "bt2020nc" if hdr else "bt709", "-c:a", "aac",
                ]
                if faststart:
                    command.extend(["-movflags", "+faststart"])
                command.append(str(source))
                subprocess.run(command, capture_output=True, check=True, timeout=30)
                original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                video_hash = downloader._source_video_packet_hash(source)
                result = downloader._prepare_source_document(source, 5_000_000, None)
                self.assertEqual(result.source_hdr, hdr)
                self.assertEqual(result.structure_modified, not faststart)
                self.assertEqual(downloader._source_video_packet_hash(result.path), video_hash)
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
                _, video, actual = downloader._source_document_probe(result.path)
                self.assertEqual(video["codec_name"], "hevc")
                self.assertEqual(video["pix_fmt"], "yuv420p10le")
                self.assertEqual(video["color_transfer"], transfer)
                self.assertEqual((video["width"], video["height"]), (96, 192))
                self.assertAlmostEqual(actual, 1, delta=0.2)
                self.assertTrue(downloader._source_document_faststart(result.path))
                downloader._source_document_decode(result.path, None)


if __name__ == "__main__":
    unittest.main()
