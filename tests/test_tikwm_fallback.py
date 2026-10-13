"""Offline checks for the anonymous TikTok resolver fallback."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from curl_cffi import requests as cffi_requests

from bot.downloader import (
    ConfirmedVideoError,
    DownloadFailedError,
    NotAVideoPostError,
    _download_tiktok_sync,
    _fetch_tiktok_media,
    _repair_tiktok_sei,
    _tiktok_item,
    _tikwm_data,
    _tikwm_media_url,
    _tikwm_photo_album,
)


class TikwmFallbackTests(unittest.TestCase):
    @patch("curl_cffi.requests.Session")
    def test_forced_retry_really_uses_alternate_before_primary(self, session_class):
        session = session_class.return_value
        session.get.return_value.text = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
            '{"__DEFAULT_SCOPE__":{"webapp.video-detail":{"itemInfo":{"itemStruct":'
            '{"id":"123","video":{"playAddr":"https://tiktokcdn.com/clip.mp4"}}}}}}'
            '</script>'
        )
        with patch("bot.downloader.proxy_ladder", return_value=["socks5h://warp:1080"]):
            _tiktok_item("https://www.tiktok.com/@example/video/123", "socks5h://goida:2079")
        self.assertEqual(session_class.call_args.kwargs["proxies"]["https"],
                         "socks5h://goida:2079")

    @patch("curl_cffi.requests.Session")
    def test_resolver_can_escape_dead_direct_and_primary(self, session_class):
        first, second, third = MagicMock(), MagicMock(), MagicMock()
        first.get.side_effect = TimeoutError()
        second.get.side_effect = TimeoutError()
        third.get.return_value = MagicMock(status_code=200, **{
            "json.return_value": {"code": 0, "data": {"id": "123"}},
        })
        session_class.side_effect = [first, second, third]
        with patch("bot.downloader.proxy_ladder", return_value=[
            "socks5h://warp:1080", "socks5h://goida:2079",
        ]):
            _, returned = _tikwm_data("https://vt.tiktok.com/test/", "socks5h://warp:1080")
        self.assertIs(returned, third)
        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertEqual(session_class.call_args.kwargs["proxies"]["https"],
                         "socks5h://goida:2079")

    @patch("curl_cffi.requests.Session")
    def test_resolver_stops_when_total_budget_is_spent(self, session_class):
        session_class.return_value.get.side_effect = TimeoutError()
        with (
            patch("bot.downloader.proxy_ladder", return_value=["socks5h://goida:2079"]),
            patch("bot.downloader.time.monotonic", side_effect=[0, 0, 46]),
            self.assertRaises(DownloadFailedError),
        ):
            _tikwm_data("https://vt.tiktok.com/test/", "socks5h://warp:1080")
        session_class.assert_called_once()

    def test_media_url_is_restricted_to_https_cdn(self):
        self.assertEqual(
            _tikwm_media_url("//v19.tiktokcdn-us.com/video.mp4"),
            "https://v19.tiktokcdn-us.com/video.mp4",
        )
        self.assertEqual(
            _tikwm_media_url("https://v45.tiktokcdn-eu.com/video.mp4"),
            "https://v45.tiktokcdn-eu.com/video.mp4",
        )
        for url in (
            "http://v19.tiktokcdn-us.com/video.mp4",
            "https://127.0.0.1/private",
            "https://tiktokcdn-us.com.evil.test/video.mp4",
            "https://user@www.tikwm.com/video.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(DownloadFailedError):
                _tikwm_media_url(url)

    @patch("curl_cffi.requests.Session")
    def test_resolver_gets_only_the_public_url(self, session_class):
        link = "https://vt.tiktok.com/ZSbh2gaG2/"
        session = session_class.return_value
        session.get.return_value = MagicMock(
            status_code=200,
            **{"json.return_value": {"code": 0, "data": {"id": "7689605066751315220"}}},
        )
        data, returned_session = _tikwm_data(link)
        self.assertEqual(data["id"], "7689605066751315220")
        self.assertIs(returned_session, session)
        session_class.assert_called_once_with(
            impersonate="firefox133", proxies=None, timeout=15,
        )
        session.get.assert_called_once_with("https://www.tikwm.com/api/", params={"url": link})

    @patch("curl_cffi.requests.Session")
    def test_resolver_retries_configured_proxy_after_direct_fails(self, session_class):
        first, second = MagicMock(), MagicMock()
        first.get.side_effect = TimeoutError("direct path timed out")
        second.get.return_value = MagicMock(
            status_code=200,
            **{"json.return_value": {"code": 0, "data": {"id": "7689605066751315220"}}},
        )
        session_class.side_effect = [first, second]
        _, returned = _tikwm_data(
            "https://vt.tiktok.com/ZSbh2gaG2/", "socks5://warp:2080",
        )
        self.assertIs(returned, second)
        first.close.assert_called_once()
        self.assertIsNone(session_class.call_args_list[0].kwargs["proxies"])
        self.assertEqual(session_class.call_args_list[1].kwargs["proxies"], {
            "http": "socks5://warp:2080", "https": "socks5://warp:2080",
        })

    def test_geo_block_uses_validated_fallback_media(self):
        link = "https://vt.tiktok.com/ZSbh2gaG2/"
        response = {
            "id": "7689605066751315220", "title": "example", "duration": 18,
            "play": "https://v19.tiktokcdn-us.com/video.mp4",
        }
        session = MagicMock()

        def write_media(_session, _url, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"video")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tiktok_item", side_effect=DownloadFailedError("geo")),
            patch("bot.downloader._tikwm_data", return_value=(response, session)),
            patch("bot.downloader._stream_to_file", side_effect=write_media) as stream,
            patch("bot.downloader._tiktok_playable", return_value=True),
        ):
            path, info = _download_tiktok_sync(link, tmpdir, None, 10_000, None, None, {})
        self.assertEqual(info["id"], response["id"])
        self.assertEqual(path.suffix, ".mp4")
        self.assertFalse(stream.call_args.kwargs["allow_redirects"])
        session.close.assert_called_once()

    def test_hollow_video_page_is_not_mistaken_for_photo(self):
        link = "https://vt.tiktok.com/ZSbh2gaG2/"
        response = {
            "id": "7689605066751315220", "play": "https://v19.tiktokcdn-us.com/video.mp4",
        }
        session = MagicMock()

        def write_media(_session, _url, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"video")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tiktok_item", side_effect=NotAVideoPostError("hollow")),
            patch("bot.downloader._tikwm_data", return_value=(response, session)),
            patch("bot.downloader._stream_to_file", side_effect=write_media),
            patch("bot.downloader._tiktok_playable", return_value=True),
        ):
            _, info = _download_tiktok_sync(link, tmpdir, None, 10_000, None, None, {})
        self.assertEqual(info["id"], response["id"])
        session.close.assert_called_once()

    @patch("curl_cffi.requests.Session")
    def test_direct_cdn_failure_tries_warp_before_resolver(self, session_class):
        link = "https://vt.tiktok.com/ZSb6B6Cef/"
        direct, resolver, warp = MagicMock(), MagicMock(), MagicMock()
        resolver.proxies = {}
        session_class.return_value = warp
        direct_item = {"id": "7686099879466224916", "video": {
            "playAddr": "https://tiktokcdn.com/direct.mp4",
        }}
        resolver_data = {
            "id": "7686099879466224916",
            "play": "https://v16m.tiktokcdn-us.com/video.mp4",
        }
        seen = []

        def fetch(session, _url, destination, *_args, **_kwargs):
            seen.append(session)
            if session is not warp:
                raise cffi_requests.exceptions.Timeout("CDN timeout")
            Path(destination).write_bytes(b"valid mp4")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tiktok_item", return_value=(direct_item, direct)),
            patch("bot.downloader._tikwm_data", return_value=(resolver_data, resolver)),
            patch("bot.downloader._stream_to_file", side_effect=fetch),
            patch("bot.downloader._tiktok_playable", return_value=True),
        ):
            _, info = _download_tiktok_sync(
                link, tmpdir, "socks5h://warp:1080", 10_000, None, None, {},
            )
        self.assertEqual(info["id"], resolver_data["id"])
        self.assertEqual(seen, [direct, warp])
        direct.close.assert_called_once()
        resolver.close.assert_not_called()
        warp.close.assert_called_once()
        session_class.assert_called_once()

    @patch("curl_cffi.requests.Session")
    def test_cdn_retry_count_is_bounded_and_sessions_closed(self, session_class):
        source = MagicMock()
        source.proxies = {}
        item = {"id": "123", "video": {"bitrateInfo": [
            {"CodecType": "h264", "PlayAddr": {"UrlList": [
                f"https://v16m.tiktokcdn-us.com/{index}.mp4" for index in range(20)
            ]}},
        ]}}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.downloader.proxy_ladder", return_value=[]),
            patch("bot.downloader._stream_to_file", return_value=False) as stream,
            self.assertRaisesRegex(ConfirmedVideoError, "лимит попыток"),
        ):
            _fetch_tiktok_media(item, source, directory, "socks5h://warp:1080",
                                1000, None, None, {}, resolver=True)
        self.assertEqual(stream.call_count, 12)
        session_class.return_value.close.assert_called_once()
        source.close.assert_not_called()

    @patch("curl_cffi.requests.Session")
    def test_complete_broken_video_uses_repaired_copy_and_keeps_original(self, session_class):
        source = MagicMock()
        source.proxies = {}
        item = {"id": "123", "video": {
            "playAddr": "https://v16m.tiktokcdn-us.com/clip.mp4",
        }}
        guard = {}

        def fetch(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"complete source")
            guard["_last_complete_body"] = True
            return True

        def repair(source_path, _max_bytes, _cancel):
            repaired = source_path.with_name("123-compatible.mp4")
            repaired.write_bytes(b"compatible copy")
            return repaired

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.downloader._stream_to_file", side_effect=fetch) as stream,
            patch("bot.downloader._tiktok_playable", side_effect=[False, True]),
            patch("bot.downloader._repair_tiktok_sei", side_effect=repair),
        ):
            path, info = _fetch_tiktok_media(
                item, source, directory, "socks5h://warp:1080",
                10_000, None, None, guard, resolver=True,
            )
            self.assertEqual(path.read_bytes(), b"compatible copy")
            self.assertEqual(info["original_path"].read_bytes(), b"complete source")
        stream.assert_called_once()
        session_class.assert_not_called()

    @patch("curl_cffi.requests.Session")
    def test_complete_broken_video_does_not_repeat_same_url_on_other_exits(self, session_class):
        source = MagicMock()
        source.proxies = {}
        guard = {}

        def fetch(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"complete but invalid")
            guard["_last_complete_body"] = True
            return True

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.downloader._stream_to_file", side_effect=fetch) as stream,
            patch("bot.downloader._tiktok_playable", return_value=False),
            patch("bot.downloader._repair_tiktok_sei", return_value=None),
            self.assertRaises(ConfirmedVideoError),
        ):
            _fetch_tiktok_media(
                {"id": "123", "video": {
                    "playAddr": "https://v16m.tiktokcdn-us.com/clip.mp4",
                }},
                source, directory, "socks5h://warp:1080", 10_000,
                None, None, guard, resolver=True,
            )
        stream.assert_called_once()
        session_class.assert_not_called()

    def test_failed_sei_copy_removes_partial_file(self):
        probe = MagicMock(returncode=0, stdout=(
            '{"format":{"format_name":"mov,mp4,m4a,3gp,3g2,mj2"},'
            '"streams":[{"codec_type":"video","codec_name":"h264"},'
            '{"codec_type":"audio","codec_name":"aac"}]}'
        ))

        def fail_after_write(command, _timeout, _cancel):
            Path(command[-1]).write_bytes(b"partial")
            from bot.media_parts import MediaPreparationError
            raise MediaPreparationError("ffmpeg failed")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.downloader.subprocess.run", return_value=probe),
            patch("bot.downloader._run_ffmpeg", side_effect=fail_after_write) as remux,
        ):
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"source")
            self.assertIsNone(_repair_tiktok_sei(source, 10_000, None))
            self.assertEqual(source.read_bytes(), b"source")
            self.assertFalse((Path(directory) / "source-compatible.mp4").exists())
        command = remux.call_args.args[0]
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertEqual(command[command.index("-bsf:v") + 1],
                         "filter_units=remove_types=6")

    @patch("curl_cffi.requests.Session")
    def test_confirmed_video_failure_does_not_become_photo(self, session_class):
        resolver, warp = MagicMock(), MagicMock()
        resolver.proxies = {}
        session_class.return_value = warp
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tiktok_item", side_effect=DownloadFailedError("geo")),
            patch("bot.downloader._tikwm_data", return_value=({
                "id": "7686099879466224916",
                "play": "https://v16m.tiktokcdn-us.com/video.mp4",
            }, resolver)),
            patch("bot.downloader._stream_to_file", return_value=False),
            self.assertRaises(ConfirmedVideoError),
        ):
            _download_tiktok_sync(
                "https://vt.tiktok.com/ZSb6B6Cef/", tmpdir,
                "socks5h://warp:1080", 10_000, None, None, {},
            )
        resolver.close.assert_called_once()
        warp.close.assert_called_once()

    def test_real_photo_stays_on_album_path(self):
        session = MagicMock()
        response = {
            "id": "7479037796326362385",
            "play": "https://v19.tiktokcdn-us.com/slideshow.mp4",
            "images": ["https://p16-common-sign.tiktokcdn-us.com/photo.jpg"],
        }
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tiktok_item", side_effect=NotAVideoPostError("photo")),
            patch("bot.downloader._tikwm_data", return_value=(response, session)),
            self.assertRaises(NotAVideoPostError),
        ):
            _download_tiktok_sync(
                "https://www.tiktok.com/@qq.mm.pp/photo/7479037796326362385",
                tmpdir, None, 10_000, None, None, {},
            )
        session.close.assert_called_once()

    def test_photo_album_requires_each_image(self):
        session = MagicMock()
        data = {
            "id": "7479037796326362385", "title": "photos",
            "images": [
                "https://p16-common-sign.tiktokcdn-us.com/one",
                "https://p16-common-sign.tiktokcdn-us.com/two",
            ],
        }

        def write_image(_session, _url, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"image")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tikwm_data", return_value=(data, session)),
            patch("bot.downloader._stream_to_file", side_effect=write_image) as stream,
            patch("bot.downloader._tikwm_photo_extension", return_value=".jpg"),
        ):
            album = _tikwm_photo_album(
                "https://www.tiktok.com/@qq.mm.pp/photo/7479037796326362385", tmpdir,
            )
            self.assertEqual([p.name for p in album.items], ["photo-1.jpg", "photo-2.jpg"])
            self.assertEqual(album.description, "photos")
            self.assertEqual(stream.call_count, 2)
            self.assertTrue(all(not call.kwargs["allow_redirects"] for call in stream.call_args_list))
        session.close.assert_called_once()

    def test_31_photo_post_keeps_every_slide_with_bounded_downloads(self):
        session = MagicMock()
        data = {
            "id": "7690633438700866834",
            "images": [
                f"https://p16-common-sign.tiktokcdn-us.com/{i}" for i in range(31)
            ],
        }

        def write_image(_session, _url, destination, *_args, **_kwargs):
            Path(destination).write_bytes(b"image")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tikwm_data", return_value=(data, session)),
            patch("bot.downloader._stream_to_file", side_effect=write_image) as stream,
            patch("bot.downloader._tikwm_photo_extension", return_value=".jpg"),
        ):
            album = _tikwm_photo_album(
                "https://www.tiktok.com/@rastro.incomum/photo/7690633438700866834",
                tmpdir,
            )
            self.assertEqual(len(album.items), 31)
            self.assertEqual(album.items[-1].name, "photo-31.jpg")
            self.assertEqual(stream.call_count, 31)
            self.assertTrue(all(call.args[3] <= 20 * 1024 * 1024
                                for call in stream.call_args_list))
        session.close.assert_called_once()

    def test_photo_album_rejects_more_than_40_before_any_download(self):
        session = MagicMock()
        data = {"images": ["https://p16-common-sign.tiktokcdn-us.com/p"] * 41}
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tikwm_data", return_value=(data, session)),
            patch("bot.downloader._stream_to_file") as stream,
            self.assertRaisesRegex(DownloadFailedError, "больше 40"),
        ):
            _tikwm_photo_album(
                "https://www.tiktok.com/@rastro.incomum/photo/7690633438700866834",
                tmpdir,
            )
        stream.assert_not_called()
        session.close.assert_called_once()

    @patch("curl_cffi.requests.Session")
    def test_photo_cdn_timeout_switches_once_to_proxy(self, session_class):
        direct, via_warp = MagicMock(), MagicMock()
        direct.proxies = {}
        via_warp.proxies = {"https": "socks5h://warp:1080"}
        session_class.return_value = via_warp
        data = {
            "id": "7479037796326362385", "images": [
                "https://p16-common-sign.tiktokcdn-us.com/one",
                "https://p16-common-sign.tiktokcdn-us.com/two",
            ],
        }
        seen_sessions = []

        def fetch(session, _url, destination, *_args, **_kwargs):
            seen_sessions.append(session)
            if session is direct:
                raise cffi_requests.exceptions.Timeout("direct CDN timeout")
            Path(destination).write_bytes(b"photo")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tikwm_data", return_value=(data, direct)),
            patch("bot.downloader._stream_to_file", side_effect=fetch),
            patch("bot.downloader._tikwm_photo_extension", return_value=".jpg"),
        ):
            album = _tikwm_photo_album(
                "https://www.tiktok.com/@qq.mm.pp/photo/7479037796326362385",
                tmpdir, "socks5h://warp:1080",
            )
            self.assertEqual(len(album.items), 2)
        self.assertEqual(seen_sessions, [direct, via_warp, via_warp])
        session_class.assert_called_once_with(
            impersonate="firefox133",
            proxies={"http": "socks5h://warp:1080", "https": "socks5h://warp:1080"},
            timeout=10,
        )
        direct.close.assert_called_once()
        via_warp.close.assert_called_once()

    @patch("curl_cffi.requests.Session")
    def test_photo_cdn_uses_xray_when_warp_cannot_connect(self, session_class):
        direct, warp, xray = MagicMock(), MagicMock(), MagicMock()
        direct.proxies = {}
        warp.proxies = {"https": "socks5h://warp:1080"}
        xray.proxies = {"https": "socks5h://xray:2075"}
        session_class.side_effect = [warp, xray]
        data = {"images": [
            "https://p16-common-sign.tiktokcdn-us.com/one",
            "https://p16-common-sign.tiktokcdn-us.com/two",
        ]}
        seen = []

        def fetch(session, _url, destination, *_args, **_kwargs):
            seen.append(session)
            if session is direct or session is warp:
                raise cffi_requests.exceptions.Timeout("CDN unavailable")
            Path(destination).write_bytes(b"photo")
            return True

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("bot.downloader._tikwm_data", return_value=(data, direct)),
            # The live TikTok policy is WARP, so its generic ladder contains
            # no Xray ports. The photo CDN must still reach the fixed exits.
            patch("bot.downloader.proxy_ladder", return_value=["socks5h://warp:1080"]),
            patch("bot.downloader._stream_to_file", side_effect=fetch),
            patch("bot.downloader._tikwm_photo_extension", return_value=".jpg"),
        ):
            album = _tikwm_photo_album(
                "https://www.tiktok.com/@rastro.incomum/photo/7690633438700866834",
                tmpdir,
            )
        self.assertEqual(len(album.items), 2)
        self.assertEqual(seen, [direct, warp, xray, xray])
        self.assertEqual(session_class.call_count, 2)
        direct.close.assert_called_once()
        warp.close.assert_called_once()
        xray.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
