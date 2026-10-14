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
    def test_dolby_vision_is_not_mislabeled_as_an_sdr_preview(self) -> None:
        video = {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                 "width": 720, "height": 1280, "color_transfer": "bt709",
                 "side_data_list": [{"side_data_type": "DOVI configuration record",
                                     "dv_profile": 9, "rpu_present_flag": 1}]}
        payload = {"streams": [video], "format": {"duration": "2", "format_name": "mp4"}}
        with patch.object(downloader.subprocess, "run", return_value=MagicMock(
            returncode=0, stdout=json.dumps(payload),
        )) as run, patch.object(downloader, "_run_ffmpeg") as encode:
            self.assertFalse(downloader._tiktok_playable(Path("hdr.mp4")))
            self.assertIsNone(downloader._transcode_tiktok_compatible(Path("hdr.mp4"), 1000, None))
            self.assertTrue(downloader._source_hdr(Path("hdr.mp4")))
        self.assertEqual(run.call_count, 3)
        encode.assert_not_called()

    def test_resolver_requests_and_preserves_distinct_hd_source(self) -> None:
        url = "https://vt.tiktok.com/example/"
        payload = {"id": "123", "play": "https://v19.tiktokcdn-us.com/preview.mp4",
                   "hdplay": "https://v19.tiktokcdn-us.com/source.mp4",
                   "size": 100, "hd_size": 500, "duration": 30}
        session = MagicMock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"code": 0, "data": payload}
        with patch("curl_cffi.requests.Session", return_value=session):
            data, _ = downloader._tikwm_data(url)
        self.assertEqual(session.get.call_args.kwargs["params"], {"url": url, "hd": "1"})
        item = downloader._tikwm_video_item(data)
        preview = downloader._tiktok_variants(item)[0]
        source = downloader._tiktok_variants(item, original_only=True)[0]
        self.assertEqual(preview["urls"], [payload["play"]])
        self.assertEqual(source["urls"], [payload["hdplay"]])
        self.assertEqual(source["size"], 500)
        self.assertEqual(source["codec"], "")
        payload["hdplay"] = payload["play"]
        self.assertEqual(len(downloader._tiktok_variants(downloader._tikwm_video_item(payload))), 1)

    def test_sdr_resolver_preview_keeps_proven_hdr_from_distinct_source(self) -> None:
        item = downloader._tikwm_video_item({
            "id": "123", "play": "https://v19.tiktokcdn-us.com/preview.mp4",
            "hdplay": "https://v19.tiktokcdn-us.com/source.mp4",
        })
        session = MagicMock(proxies={})

        def fetch(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"SDR preview")
            return True

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(downloader, "_stream_to_file", side_effect=fetch), \
             patch.object(downloader, "_tiktok_playable", return_value=True), \
             patch("bot.tiktok_hdr.probe_tiktok_item_hdr", return_value=True) as probe:
            _, info = downloader._fetch_tiktok_media(
                item, session, directory, None, 10_000, None, None, {}, resolver=True,
            )
        self.assertIs(info["source_hdr"], True)
        self.assertFalse(info["original_matches_preview"])
        probe.assert_called_once_with(item, session)

    def test_original_file_prefers_proven_hdr_even_below_highest_sdr(self) -> None:
        sdr_url = "https://v19.tiktokcdn-us.com/sdr.mp4"
        hdr_url = "https://v19.tiktokcdn-us.com/hdr.mp4"
        for listing in ("listed", "outside", "mirror"):
            entries = [{"CodecType": "h264", "Bitrate": 10, "PlayAddr": {
                "UrlList": [sdr_url], "Width": 2160, "Height": 3840,
            }}]
            if listing == "listed":
                entries.append({"CodecType": "h265", "Bitrate": 5, "PlayAddr": {
                    "UrlList": [hdr_url], "Width": 1080, "Height": 1920,
                }})
            elif listing == "mirror":
                entries[0]["PlayAddr"]["UrlList"].append(hdr_url)
            item = {"id": "123", "video": {"bitrateInfo": entries}}
            guard = {}

            def proof(video_item, _session):
                video_item["video"]["_verified_hdr_urls"] = [hdr_url]
                return True

            def fetch(_session, url, destination, *_args, transfer_guard=guard, **_kwargs):
                self.assertEqual(url, hdr_url)
                destination.write_bytes(b"actual HDR")
                transfer_guard["_last_complete_body"] = True
                return True

            with self.subTest(listing=listing), tempfile.TemporaryDirectory() as directory, \
                 patch("bot.tiktok_hdr.probe_tiktok_item_hdr", side_effect=proof), \
                 patch.object(downloader, "_stream_to_file", side_effect=fetch), \
                 patch.object(downloader, "_tiktok_original_valid", return_value=True), \
                 patch.object(downloader, "_source_hdr_state", return_value=True), \
                 patch.object(downloader, "_transcode_tiktok_compatible") as encode:
                _, info = downloader._fetch_tiktok_media(
                    item, MagicMock(proxies={}), directory, None, 10_000, None, None,
                    guard, resolver=False, original_only=True,
                )
            self.assertIs(info["source_hdr"], True)
            encode.assert_not_called()

    def test_conversion_budget_accounts_for_actual_4k60_and_stays_bounded(self) -> None:
        low = {"width": 960, "height": 1920, "pix_fmt": "yuv420p10le",
               "avg_frame_rate": "30/1"}
        high = {**low, "width": 2160, "height": 3840, "avg_frame_rate": "60/1"}
        self.assertAlmostEqual(downloader._tiktok_conversion_budget(low, 32.5), 390)
        self.assertAlmostEqual(downloader._tiktok_conversion_budget(high, 11.96425), 1292.139)
        self.assertEqual(downloader._tiktok_conversion_budget(high, 3600), 1800)
        self.assertEqual(downloader._tiktok_conversion_budget({}, 2), 120)
        high["avg_frame_rate"] = "0/0"
        high["r_frame_rate"] = "60/1"
        self.assertAlmostEqual(downloader._tiktok_conversion_budget(high, 11.96425), 1292.139)
        high["pix_fmt"] = "yuv420p12le"
        self.assertAlmostEqual(downloader._tiktok_conversion_budget(high, 11.96425), 1550.5668)
        self.assertEqual(downloader._tiktok_validation_budget([high] * 2, 3600), 600)
        self.assertEqual(downloader._tiktok_validation_budget([{}], 2), 90)
        self.assertEqual(downloader._tiktok_validation_budget([{}], 2, floor=120), 120)

    def test_faster_preset_requires_room_without_changing_crf_or_geometry(self) -> None:
        for limit, preset in ((900, "veryfast"), (1000, "ultrafast")):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"x" * 100)

                def encode(command, *_args):
                    Path(command[-1]).write_bytes(b"compatible")

                with patch.object(downloader.subprocess, "run", side_effect=[
                    _probe("arib-std-b67"),
                    MagicMock(returncode=0, stdout='{"format":{"duration":"2"}}'),
                ]), patch.object(downloader, "_run_ffmpeg", side_effect=encode) as run:
                    result = downloader._transcode_tiktok_compatible(source, limit, None)
                    self.assertIsNotNone(result)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("-preset") + 1], preset)
                self.assertEqual(command[command.index("-crf") + 1], "18")
                self.assertNotIn("-r", command)
                self.assertIn("trunc(iw/2)*2", command[command.index("-vf") + 1])

    def test_media_hdr_known_state_does_not_mislabel_lower_sdr_preview(self) -> None:
        cases = ((True, False, True, True), (False, False, False, True),
                 (None, False, False, False), (None, True, False, False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.mp4"
            path.write_bytes(b"SDR preview")
            for native, matches, hdr, verified in cases:
                with self.subTest(native=native, matches=matches), \
                     patch.object(downloader, "_ffprobe_dimensions", return_value=(540, 960)), \
                     patch.object(downloader, "_source_hdr_state", return_value=False):
                    media = downloader._build_media(path, {"source_hdr": native,
                        "original_matches_preview": matches})
                self.assertEqual(media.source_hdr, hdr)
                self.assertEqual(media.source_hdr_verified, verified)

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
             patch("bot.tiktok_hdr.probe_tiktok_item_hdr", return_value=None), \
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

    def test_original_variants_prefer_native_geometry_then_bitrate(self) -> None:
        item = {"video": {"bitrateInfo": [
            {"CodecType": "h264", "Bitrate": 9_000_000, "PlayAddr": {
                "UrlList": ["sdr"], "Width": 540, "Height": 960,
            }},
            {"CodecType": "h265", "Bitrate": 5_000_000, "PlayAddr": {
                "UrlList": ["hdr"], "Width": 960, "Height": 1920,
            }},
            {"CodecType": "h265", "Bitrate": 4_000_000, "PlayAddr": {
                "UrlList": ["lower-bitrate"], "Width": 960, "Height": 1920,
            }},
        ]}}
        self.assertEqual(downloader._tiktok_variants(item)[0]["urls"], ["sdr"])
        self.assertEqual(downloader._tiktok_variants(item, original_only=True)[0]["urls"], ["hdr"])

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
             patch("bot.tiktok_hdr.probe_tiktok_item_hdr", return_value=None), \
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
                 patch.object(downloader, "_transcode_tiktok_compatible", side_effect=convert), \
                 patch.object(downloader, "_source_hdr_state", return_value=True):
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
