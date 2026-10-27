"""Source documents preserve HDR/video packets while repairing MP4 packaging."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import struct
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from bot import downloader
from bot.media_parts import MediaPreparationError


class SourceDocumentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
    def test_real_unspecified_nal_sample_is_removed_without_changing_valid_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty-sample.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'testsrc2=size=96x192:rate=15:duration=1', '-c:v', 'libx264',
                            '-threads', '1', '-g', '1', '-movflags', '+faststart', str(path)],
                           capture_output=True, check=True, timeout=30)
            packets = json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                '-show_entries', 'packet=pos,size', '-of', 'json', str(path)]))['packets']
            packet = packets[len(packets) // 2]
            at, size = int(packet['pos']), int(packet['size'])
            raw = bytearray(path.read_bytes())
            raw[at:at + size] = (size - 4).to_bytes(4, 'big') + bytes(size - 4)
            path.write_bytes(raw)
            # Reproduce a complete container containing a sample with no picture.
            with self.assertRaises(MediaPreparationError):
                downloader._source_document_decode(path, None)
            expected = downloader._source_video_packet_hash(path, remove_unspecified=True)
            result = downloader._prepare_source_document(path, 5_000_000, None)
            self.assertTrue(result.structure_modified)
            self.assertEqual(downloader._source_video_packet_hash(result.path), expected)
            downloader._source_document_decode(result.path, None)

    def test_movie_header_unknown_duration_is_not_a_healthy_faststart(self):
        def box(kind, payload):
            return struct.pack('>I4s', len(payload) + 8, kind) + payload
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'movie.mp4'
            for version in (0, 1):
                for duration, expected in ((48_000, True), (0, False),
                                           (2 ** (64 if version else 32) - 1, False)):
                    header = bytes([version, 0, 0, 0]) + bytes(16 if version else 8)
                    header += struct.pack('>I', 48_000)
                    header += duration.to_bytes(8 if version else 4, 'big')
                    path.write_bytes(box(b'ftyp', b'isom') + box(b'moov', box(b'mvhd', header))
                                     + box(b'mdat', b'packets'))
                    self.assertEqual(downloader._source_document_faststart(path), expected)

    def test_remux_cannot_report_hdr_if_dolby_vision_metadata_was_lost(self) -> None:
        video = {"codec_type": "video", "codec_name": "hevc", "width": 720, "height": 1280,
                 "pix_fmt": "yuv420p10le", "color_transfer": "bt709",
                 "side_data_list": [{"side_data_type": "DOVI configuration record",
                                     "dv_profile": 8, "rpu_present_flag": 1}]}
        stripped = {key: value for key, value in video.items() if key != "side_data_list"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original Dolby Vision")

            def remux(command, *_args):
                Path(command[-1]).write_bytes(b"same video packets but missing DV configuration")

            with patch.object(downloader, "_source_document_probe", side_effect=[
                ({"streams": [video]}, video, 2), ({"streams": [stripped]}, stripped, 2),
            ]), patch.object(downloader, "_source_document_faststart", return_value=False), \
                 patch.object(downloader, "_source_document_decode") as decode, \
                 patch.object(downloader, "_run_ffmpeg", side_effect=remux), \
                 patch.object(downloader.shutil, "disk_usage") as usage, \
                 self.assertRaisesRegex(downloader.DownloadFailedError, "изменила параметры"):
                usage.return_value.free = 10 * 1024**3
                downloader._prepare_source_document(source, 100_000, None)
            decode.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original Dolby Vision")
            self.assertFalse(source.with_name("source-source.mp4").exists())

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
        for transfer, faststart, bad_duration in (("arib-std-b67", True, False),
                ("smpte2084", False, False), ("bt709", False, False),
                ("arib-std-b67", True, True)):
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
                if bad_duration:
                    raw = bytearray(source.read_bytes())
                    at = raw.index(b'mvhd')
                    self.assertEqual(raw[at + 4], 0)
                    raw[at + 20:at + 24] = b'\xff' * 4
                    source.write_bytes(raw)
                original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                video_hash = downloader._source_video_packet_hash(source)
                result = downloader._prepare_source_document(source, 5_000_000, None)
                self.assertEqual(result.source_hdr, hdr)
                self.assertEqual(result.structure_modified, not faststart or bad_duration)
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
