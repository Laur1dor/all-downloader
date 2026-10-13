"""Spotify fallback must reject a similarly named but different recording."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot import downloader
from bot.progress import PHASE_DOWNLOAD, ProgressState


class SpotifyFallbackTests(unittest.TestCase):
    def test_public_metadata_and_strict_match(self) -> None:
        embed = Mock()
        embed.json.return_value = {"title": "Musica 2026"}
        page = Mock()
        page.text = (
            '<meta property="og:title" content="Musica 2026">'
            '<meta property="og:description" content="A-Clark, VINNY, Fly Project '
            '· Musica 2026 · Song · 2026">'
            '<meta name="music:duration" content="163">'
        )
        page.iter_content.return_value = [page.text.encode()]
        candidates = {"entries": [
            {"title": "Musica 2026", "uploader": "Unrelated", "duration": 163,
             "webpage_url": "https://youtu.be/wrong-artist"},
            {"title": "A Clark, VINNY, Fly Project – Musica 2026",
             "uploader": "Fly Project", "duration": 163,
             "webpage_url": "https://youtu.be/correct"},
        ]}
        with patch.object(downloader, "_curl_get", side_effect=[embed, page]), \
             patch.object(downloader, "_extract_info", return_value=candidates), \
             patch.object(downloader, "_base_options", return_value={}):
            self.assertEqual(
                downloader._spotify_fallback_video(
                    "https://open.spotify.com/track/0PrOJ5GBs8iSmPsc1b16vE"
                ),
                ("https://youtu.be/correct", "Musica 2026"),
            )

    def test_metadata_tries_next_exit_after_generic_page(self) -> None:
        embed = Mock()
        embed.json.return_value = {"title": "Maybe"}
        generic = Mock()
        generic.text = '<meta property="og:description" content="Spotify is music">'
        generic.iter_content.return_value = [generic.text.encode()]
        valid = Mock()
        valid.text = (
            '<meta property="og:title" content="Maybe">'
            '<meta property="og:description" content="june · Maybe · Song · 2026">'
            '<meta name="music:duration" content="177">'
        )
        valid.iter_content.return_value = [valid.text.encode()]
        with patch.object(
            downloader, "_curl_get", side_effect=[embed, generic, OSError(), valid]
        ) as request, patch.object(
            downloader, "_SPOTIFY_METADATA_PROXY_PORTS", "2077,2075"
        ):
            self.assertEqual(
                downloader._spotify_fallback_metadata(
                    "https://open.spotify.com/track/3qvl8d54B5ckrvVhsA083u"
                ),
                ("Maybe", "june", 177),
            )
        self.assertEqual(request.call_args.kwargs["proxy"], "socks5h://xray:2075")
        self.assertLessEqual(request.call_args.kwargs["timeout"], 4)

    def test_oembed_timeout_uses_verified_track_page(self) -> None:
        page = Mock()
        page.iter_content.return_value = [
            (
                b'<meta property="og:title" content="Maybe">'
                b'<meta property="og:description" content="june \xc2\xb7 Maybe \xc2\xb7 Song">'
                b'<meta name="music:duration" content="177">'
            )
        ]
        with patch.object(downloader, "_curl_get", side_effect=[TimeoutError(), page]):
            self.assertEqual(
                downloader._spotify_fallback_metadata(
                    "https://open.spotify.com/track/3qvl8d54B5ckrvVhsA083u"
                ),
                ("Maybe", "june", 177),
            )

    def test_rejects_wrong_duration(self) -> None:
        candidate = {"entries": [{
            "title": "Maybe", "uploader": "june - Topic", "duration": 250,
            "webpage_url": "https://youtu.be/wrong-version",
        }]}
        with patch.object(downloader, "_spotify_fallback_metadata",
                          return_value=("Maybe", "june", 177)), \
             patch.object(downloader, "_extract_info", return_value=candidate), \
             patch.object(downloader, "_base_options", return_value={}), \
             self.assertRaises(downloader.DownloadFailedError):
            downloader._spotify_fallback_video("https://open.spotify.com/track/test")

    def test_spotdl_failure_uses_verified_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            def write_audio(*_args, **_kwargs):
                _args[0]["progress_hooks"][0]({
                    "status": "downloading", "downloaded_bytes": 12345,
                })
                Path(tmpdir, "fallback.mp3").write_bytes(b"audio")

            failed = subprocess.CompletedProcess([], 1, "", "parser error")
            with patch.object(downloader.subprocess, "run", return_value=failed), \
                 patch.object(downloader, "_spotify_fallback_video",
                              return_value=("https://youtu.be/correct", "Musica 2026")), \
                 patch.object(downloader, "_extract_info", side_effect=write_audio) as extract, \
                 patch.object(downloader, "_base_options", return_value={}):
                progress = ProgressState()
                result = downloader._download_spotify_sync(
                    "https://open.spotify.com/track/test", tmpdir, 1024, progress
                )
            self.assertEqual(result[0].name, "fallback.mp3")
            self.assertEqual(result[1], "Musica 2026")
            options = extract.call_args.args[0]
            self.assertEqual(options["proxy"], downloader._SPOTIFY_AUDIO_PROXY)
            self.assertEqual(options["retries"], 0)
            self.assertEqual(options["fragment_retries"], 0)
            self.assertEqual(options["socket_timeout"], 12)
            self.assertEqual(extract.call_args.kwargs["attempts"], 1)
            self.assertEqual(progress.phase, PHASE_DOWNLOAD)
            self.assertEqual(progress.downloaded, 12345)

    def test_fallback_download_error_is_user_friendly(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "parser error")
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.object(downloader.subprocess, "run", return_value=failed), \
             patch.object(downloader, "_spotify_fallback_video",
                          return_value=("https://youtu.be/correct", "Musica 2026")), \
             patch.object(downloader, "_extract_info",
                          side_effect=downloader.YtdlpDownloadError("timed out")), \
             patch.object(downloader, "_base_options", return_value={}), \
             self.assertRaisesRegex(downloader.DownloadFailedError, "Spotify"):
            downloader._download_spotify_sync(
                "https://open.spotify.com/track/test", tmpdir, 1024
            )


class SpotifyMediaTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_title_reaches_media(self) -> None:
        def fake_download(_url: str, tmpdir: str, _max_bytes: int, _progress=None):
            path = Path(tmpdir, "fallback.mp3")
            path.write_bytes(b"audio")
            return path, "Musica 2026"

        with patch.object(downloader, "_download_spotify_sync", side_effect=fake_download):
            async with downloader._spotify_download("https://open.spotify.com/track/test", 1024) as media:
                self.assertEqual(media.title, "Musica 2026")
