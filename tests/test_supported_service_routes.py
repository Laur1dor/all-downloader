"""Keep a CI case for each supported platform's routing decisions."""

import unittest

from bot.downloader import (
    PHOTO_CAPABLE_PLATFORMS,
    _PLATFORM_DOMAINS,
    detect_platform,
    is_audio_only_platform,
)
from bot.handlers.download import _description_capable, _should_offer_quality


class SupportedServiceRoutesTests(unittest.TestCase):
    def test_service_routes(self):
        # Platform, URL, audio-only, album fallback, quality menu, description.
        cases = (
            ("youtube", "https://youtu.be/abcdefghijk", False, False, True, False),
            ("youtube", "https://youtube.com/shorts/abcdefghijk", False, False, False, True),
            ("tiktok", "https://vt.tiktok.com/example/", False, True, False, True),
            ("instagram", "https://instagram.com/p/abcdefghijk/", False, True, False, True),
            ("twitter", "https://x.com/example/status/123", False, True, True, True),
            ("pinterest", "https://pin.it/example", False, True, True, False),
            ("pornhub", "https://pornhub.com/view_video.php?viewkey=example", False, False, True, False),
            ("rule34video", "https://rule34video.com/video/123/", False, False, True, False),
            ("rule34", "https://rule34.xxx/index.php?page=post&s=view&id=1", False, True, True, False),
            ("joidb", "https://the-joi-database.com/watch/example", False, False, True, False),
            ("soundcloud", "https://soundcloud.com/example/track", True, False, False, False),
            ("spotify", "https://open.spotify.com/track/example", True, False, False, False),
            ("yandexmusic", "https://music.yandex.ru/album/1/track/2", True, False, False, False),
        )
        self.assertEqual({row[0] for row in cases}, set(_PLATFORM_DOMAINS.values()))
        for platform, url, audio, album, quality, description in cases:
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), platform)
                self.assertEqual(is_audio_only_platform(platform), audio)
                self.assertEqual(platform in PHOTO_CAPABLE_PLATFORMS, album)
                self.assertEqual(not audio and _should_offer_quality(url, platform), quality)
                self.assertEqual(_description_capable(url, platform), description)
