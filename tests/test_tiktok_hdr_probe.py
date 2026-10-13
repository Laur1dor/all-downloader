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
