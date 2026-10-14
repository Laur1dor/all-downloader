import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot import tiktok_hdr as hdr


class TikTokHdrProbeTests(unittest.TestCase):
    def test_only_explicit_transfer_establishes_hdr_or_sdr(self):
        for transfer, expected in (
            ("arib-std-b67", True),
            ("smpte2084", True),
            ("bt709", False),
            ("unknown", None),
            (None, None),
        ):
            result = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "streams": [
                            {
                                "color_transfer": transfer,
                                "codec_name": "hevc",
                                "pix_fmt": "yuv420p10le",
                            }
                        ]
                    }
                ),
            )
            with (
                self.subTest(transfer=transfer),
                patch.object(hdr.subprocess, "run", return_value=result),
            ):
                self.assertIs(hdr._transfer(Path("sample.mp4"), time.monotonic() + 1), expected)

    def test_ignored_range_is_closed_without_reading_full_video(self):
        response = Mock(status_code=200, headers={})
        session = Mock()
        session.get.return_value = response
        with patch.object(hdr, "_validate_url"), self.assertRaises(ValueError):
            hdr._read_range(session, "https://cdn.test/video.mp4", 0, 255, time.monotonic() + 1)
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_range_mismatch_or_truncation_never_becomes_sdr(self):
        for header, chunks in (
            ("bytes 0-255/1024", [b"x" * 128]),
            ("bytes 1-256/1024", [b"x" * 256]),
            ("bytes 0-255/1024", [b"x" * 257]),
        ):
            response = Mock(status_code=206, headers={"Content-Range": header})
            response.iter_content.return_value = chunks
            session = Mock()
            session.get.return_value = response
            with (
                self.subTest(header=header, length=len(chunks[0])),
                patch.object(hdr, "_validate_url"),
                self.assertRaises(ValueError),
            ):
                hdr._read_range(session, "https://cdn.test/video.mp4", 0, 255, time.monotonic() + 1)
            response.close.assert_called_once()

    def test_moov_tail_keeps_original_offsets_and_one_megabyte_budget(self):
        item = {"video": {"playAddr": "https://v16.tiktokcdn.com/source.mp4"}}
        header = b"h" * hdr._FIRST_BYTES
        total = 70 * 1024**2
        tail = b"t" * (hdr._MAX_BYTES - len(header))
        positions = []

        def inspect(path, deadline):
            positions.append(path.stat().st_size)
            if len(positions) == 1:
                return None
            with path.open("rb") as source:
                self.assertEqual(source.read(1), b"h")
                source.seek(total - len(tail))
                self.assertEqual(source.read(1), b"t")
            return True

        with (
            patch.object(hdr, "_read_range", side_effect=[(header, total), (tail, total)]) as read,
            patch.object(hdr, "_transfer", side_effect=inspect),
        ):
            self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), True)
        self.assertEqual(positions, [len(header), total])
        self.assertEqual(read.call_args.args[2:4], (total - len(tail), total - 1))

    def test_timeout_and_missing_original_stay_unknown(self):
        self.assertIsNone(hdr.probe_tiktok_item_hdr({}, Mock()))
        item = {"video": {"playAddr": "https://v16.tiktokcdn.com/source.mp4"}}
        with patch.object(hdr, "_read_range", side_effect=TimeoutError):
            self.assertIsNone(hdr.probe_tiktok_item_hdr(item, Mock()))

    def test_private_or_untrusted_hosts_are_not_probed(self):
        for url in (
            "http://127.0.0.1/a",
            "https://tiktokcdn.com.attacker.test/a",
            "https://user@www.tiktok.com/a",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                hdr._validate_url(url, media=True)

    def test_sdr_first_rendition_does_not_hide_later_actual_hdr(self):
        item = {
            "video": {
                "bitrateInfo": [
                    {
                        "Bitrate": 2000000,
                        "CodecType": "h264",
                        "PlayAddr": {
                            "UrlList": ["https://v16.tiktokcdn.com/preview.mp4"],
                            "Width": 1080,
                            "Height": 1920,
                        },
                    },
                    {
                        "Bitrate": 1000000,
                        "CodecType": "h264",
                        "PlayAddr": {
                            "UrlList": ["https://v16.tiktokcdn.com/hdr.mp4"],
                            "Width": 1080,
                            "Height": 1920,
                        },
                    },
                ]
            }
        }
        with patch.object(hdr, "_probe_media_source", side_effect=[False, True]):
            self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), True)
        self.assertEqual(item["video"]["_verified_hdr_urls"], ["https://v16.tiktokcdn.com/hdr.mp4"])

    def test_failed_first_mirror_tries_second_without_sdr_assumption(self):
        item = {
            "video": {
                "bitrateInfo": [
                    {
                        "PlayAddr": {
                            "UrlList": [
                                "https://v16.tiktokcdn.com/denied.mp4",
                                "https://v19.tiktokcdn.com/hdr.mp4",
                            ]
                        }
                    }
                ]
            }
        }
        with patch.object(hdr, "_probe_media_source", side_effect=[ValueError, True]):
            self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), True)
        self.assertEqual(item["video"]["_verified_hdr_urls"], ["https://v19.tiktokcdn.com/hdr.mp4"])

    def test_sdr_transcodes_do_not_certify_unknown_original(self):
        for encoded, expected in (("normal", None), ("original", False)):
            item = {
                "video": {"playAddr": "https://v16.tiktokcdn.com/sdr.mp4", "encodedType": encoded}
            }
            with (
                self.subTest(encoded=encoded),
                patch.object(hdr, "_probe_media_source", return_value=False),
            ):
                self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), expected)

    def test_direct_original_address_is_checked_beside_bitrate_ladder(self):
        item = {
            "video": {
                "playAddr": "https://v16.tiktokcdn.com/original.mp4",
                "bitrateInfo": [{"PlayAddr": {"UrlList": ["https://v16.tiktokcdn.com/sdr.mp4"]}}],
            }
        }
        with patch.object(hdr, "_probe_media_source", side_effect=[False, True]):
            self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), True)
        self.assertEqual(
            item["video"]["_verified_hdr_urls"], ["https://v16.tiktokcdn.com/original.mp4"]
        )

    def test_dolby_vision_configuration_is_authoritative_beyond_sdr_base_transfer(self):
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "color_transfer": "bt709",
                            "side_data_list": [
                                {
                                    "side_data_type": "DOVI configuration record",
                                    "dv_profile": 8,
                                    "rpu_present_flag": 1,
                                }
                            ],
                        }
                    ]
                }
            ),
        )
        with patch.object(hdr.subprocess, "run", return_value=result):
            self.assertIs(hdr._transfer(Path("sample.mp4"), time.monotonic() + 1), True)

    def test_unknown_web_original_allows_actual_hdr_from_hd_resolver(self):
        native = {"id": "123", "video": {"encodedType": "normal"}}
        resolved = {"id": "123", "video": {"_resolver_sources": []}}
        with (
            patch("curl_cffi.requests.Session") as session,
            patch.object(hdr, "_validate_url"),
            patch("bot.downloader._tiktok_cookies", return_value={}),
            patch.object(hdr, "_native_item", return_value=native),
            patch.object(hdr, "_resolver_item", return_value=resolved),
            patch.object(hdr, "probe_tiktok_item_hdr", side_effect=[None, True]) as probe,
        ):
            self.assertIs(
                hdr._probe_url("https://www.tiktok.com/@test/video/123", None, None), True
            )
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(session.call_count, 2)

    def test_hd_resolver_gets_public_url_without_account_cookies(self):
        payload = {
            "code": 0,
            "data": {"id": "123", "play": "/video/123.mp4", "hdplay": "/video/123-hd.mp4"},
        }
        response = Mock(status_code=200)
        response.iter_content.return_value = [json.dumps(payload).encode()]
        with patch("curl_cffi.requests.Session") as factory:
            factory.return_value.get.return_value = response
            item = hdr._resolver_item(
                "https://www.tiktok.com/@test/video/123", "123", time.monotonic() + 2
            )
        self.assertEqual(len(item["video"]["_resolver_sources"]), 2)
        self.assertEqual(
            factory.return_value.get.call_args.kwargs["params"],
            {"url": "https://www.tiktok.com/@test/video/123", "hd": "1"},
        )
        factory.return_value.cookies.update.assert_not_called()
        response.close.assert_called_once()

    def test_relative_hd_hdr_uses_fresh_anonymous_media_session(self):
        from bot.downloader import _tikwm_video_item

        native_session, anonymous_session = Mock(), Mock()
        anonymous_session.cookies = {}
        response = Mock(status_code=206, headers={"Content-Range": "bytes 0-262143/300000"})
        response.iter_content.return_value = [b"x" * 16384] * 16
        anonymous_session.get.return_value = response
        item = _tikwm_video_item({"id": "123", "hdplay": "/video/123-hd.mp4"})
        metadata = SimpleNamespace(
            returncode=0, stdout=json.dumps({"streams": [{"color_transfer": "arib-std-b67"}]})
        )
        with (
            patch("curl_cffi.requests.Session", side_effect=[native_session, anonymous_session]),
            patch.object(hdr, "ensure_public_url"),
            patch(
                "bot.downloader._tiktok_cookies", return_value={"sessionid": "private-test-cookie"}
            ),
            patch.object(hdr, "_native_item", return_value=None),
            patch.object(hdr, "_resolver_item", return_value=item),
            patch.object(hdr.subprocess, "run", return_value=metadata),
        ):
            self.assertIs(
                hdr._probe_url("https://www.tiktok.com/@test/video/123", None, None), True
            )
        self.assertEqual(
            anonymous_session.get.call_args.args[0], "https://www.tikwm.com/video/123-hd.mp4"
        )
        self.assertFalse(anonymous_session.get.call_args.kwargs["allow_redirects"])
        self.assertEqual(anonymous_session.cookies, {})
        native_session.get.assert_not_called()

    def test_native_cookie_session_cannot_fetch_resolver_media(self):
        session = Mock()
        session.cookies = {"sessionid": "private-test-cookie"}
        session._tiktok_hdr_anonymous_resolver = True
        with self.assertRaises(ValueError):
            hdr._read_range(
                session, "https://www.tikwm.com/video/123.mp4", 0, 255, time.monotonic() + 1
            )
        session.get.assert_not_called()

    def test_resolver_media_permission_does_not_allow_other_subdomains(self):
        with self.assertRaises(ValueError):
            hdr._validate_url(
                "https://untrusted.tikwm.com/video/123.mp4", media=True, anonymous_resolver=True
            )

    def test_hevc_candidate_after_four_sdr_renditions_is_probed_first(self):
        entries = [
            {
                "CodecType": "h264",
                "Bitrate": 1000 - i,
                "PlayAddr": {"UrlList": [f"https://v16.tiktokcdn.com/sdr-{i}.mp4"]},
            }
            for i in range(4)
        ]
        entries.append(
            {
                "CodecType": "h265_hvc1",
                "Bitrate": 100,
                "PlayAddr": {"UrlList": ["https://v16.tiktokcdn.com/potential-hdr.mp4"]},
            }
        )
        item = {"video": {"bitrateInfo": entries}}
        with patch.object(hdr, "_probe_media_source", return_value=True) as probe:
            self.assertIs(hdr.probe_tiktok_item_hdr(item, Mock()), True)
        self.assertEqual(probe.call_args.args[0], "https://v16.tiktokcdn.com/potential-hdr.mp4")
