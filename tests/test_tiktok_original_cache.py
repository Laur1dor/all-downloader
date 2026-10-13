"""The file button can use media already downloaded for a TikTok video."""

import tempfile
import time
import unittest
from pathlib import Path

from bot.handlers import download as handler


class TikTokOriginalCacheTests(unittest.TestCase):
    def tearDown(self):
        handler._tiktok_originals.clear()
        if handler._tiktok_original_dir is not None:
            handler._tiktok_original_dir.cleanup()
            handler._tiktok_original_dir = None

    def test_original_remains_after_download_temp_file_is_removed(self):
        url = "https://www.tiktok.com/@user/video/7689605066751315220"
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.mp4"
            source.write_bytes(b"original MP4 bytes")
            handler._remember_tiktok_original(url, source)
        retained = handler._tiktok_original_path(url)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.read_bytes(), b"original MP4 bytes")
        handler._prune_tiktok_originals(time.monotonic() + handler._TIKTOK_ORIGINAL_TTL + 1)
        self.assertIsNone(handler._tiktok_original_path(url))


if __name__ == "__main__":
    unittest.main()
