"""Official anonymous Yandex Music preview resolution."""

import hashlib
import json
import unittest
from unittest.mock import patch

from bot.yandex_preview import PreviewUnavailableError, resolve_yandex_preview


class YandexPreviewTests(unittest.TestCase):
    URL = "https://music.yandex.ru/album/43018289/track/153541898"

    def _metadata(self, url: str) -> bytes:
        if url.endswith("/tracks/153541898"):
            return json.dumps({"result": [{
                "id": 153541898, "title": "Sample", "artists": [{"name": "Artist"}],
                "albums": [{"id": 43018289}],
            }]}).encode()
        if url.endswith("/download-info"):
            return json.dumps({"result": [{
                "codec": "mp3", "preview": True,
                "downloadInfoUrl": "https://storage.mds.yandex.net/download-info/abc",
            }]}).encode()
        return (b"<download-info><host>get-mp3.music.yandex.net</host>"
                b"<path>/preview.mp3</path><ts>123a5</ts><region>225</region>"
                b"<s>salt</s></download-info>")

    def test_resolves_official_30_second_preview(self) -> None:
        with patch("bot.yandex_preview._read_metadata", side_effect=self._metadata):
            preview = resolve_yandex_preview(self.URL)
        digest = hashlib.md5(
            b"XGRlBW9FXlekgbPrRHuSiApreview.mp3salt"
        ).hexdigest()
        self.assertEqual(preview.url,
                         f"https://get-mp3.music.yandex.net/get-mp3/"
                         f"{digest}/123a5/preview.mp3?track-id=153541898")
        self.assertEqual(preview.title, "Artist — Sample")
        self.assertEqual(preview.duration, 30)

    def test_rejects_non_preview_variant(self) -> None:
        def metadata(url: str) -> bytes:
            if url.endswith("/download-info"):
                return json.dumps({"result": [{
                    "codec": "mp3", "preview": False,
                    "downloadInfoUrl": "https://storage.mds.yandex.net/download-info/abc",
                }]}).encode()
            return self._metadata(url)

        with (
            patch("bot.yandex_preview._read_metadata", side_effect=metadata),
            self.assertRaises(PreviewUnavailableError),
        ):
            resolve_yandex_preview(self.URL)

    def test_rejects_private_media_host(self) -> None:
        def metadata(url: str) -> bytes:
            if "storage.mds.yandex.net" in url:
                return (b"<download-info><host>127.0.0.1</host>"
                        b"<path>/preview.mp3</path><ts>12345</ts>"
                        b"<s>salt</s></download-info>")
            return self._metadata(url)

        with (
            patch("bot.yandex_preview._read_metadata", side_effect=metadata),
            self.assertRaises(PreviewUnavailableError),
        ):
            resolve_yandex_preview(self.URL)


if __name__ == "__main__":
    unittest.main()
