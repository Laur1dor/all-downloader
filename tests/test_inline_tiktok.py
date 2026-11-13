import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import download
from bot.urlcache import UrlCache


LINK = "https://www.tiktok.com/@example/video/7412345678901234567"


class InlineTikTokTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_result_opens_private_chat_with_original_url(self):
        cache = UrlCache()
        query = SimpleNamespace(query=LINK, from_user=SimpleNamespace(id=2),
                                answer=AsyncMock())
        db = SimpleNamespace(get_cached_file=AsyncMock(return_value={
            "file_id": "oversized", "file_size": 75 * 1024**2,
        }))
        with patch.object(download, "_inline_fallbacks", download.OrderedDict()), \
             patch.object(download, "_user_limit", return_value=2 * 1024**3):
            await download.handle_inline_tiktok(query, db, Mock(), cache)
        self.assertEqual(query.answer.await_args.args[0], [])
        button = query.answer.await_args.kwargs["button"]
        self.assertIn("75,00 МиБ", button.text)
        self.assertEqual(cache.get(button.start_parameter.removeprefix("inline_")), LINK)

    async def test_cold_oversized_video_edits_progress_message_with_private_link(self):
        cache = UrlCache()
        bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
                              edit_message_text=AsyncMock(), send_video=AsyncMock())
        db = SimpleNamespace(get_cached_file=AsyncMock(return_value=None))

        @asynccontextmanager
        async def oversized(*args, **kwargs):
            raise download.OversizedError(83 * 1024**2, kwargs["max_bytes"])
            yield

        with patch.object(download, "_inline_fallbacks", download.OrderedDict()), \
             patch.object(download, "_download_limit", return_value=100 * 1024**2), \
             patch.object(download, "_user_limit", return_value=100 * 1024**2), \
             patch.object(download, "ensure_public_url"), \
             patch.object(download, "download_video", oversized):
            await download._prepare_inline_tiktok(bot, db, LINK, Mock(), 2, -100123456)
            await download.finish_inline_tiktok(SimpleNamespace(
                query=LINK, inline_message_id="inline-id", bot=bot,
                from_user=SimpleNamespace(id=2),
            ), db, Mock(), cache)

        self.assertIn("83,00 МиБ", bot.edit_message_text.await_args.args[0])
        button = bot.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(cache.get(button.url.split("start=inline_", 1)[1]), LINK)
        bot.send_video.assert_not_awaited()

    async def test_registered_platforms_return_cached_media(self):
        from bot.downloader import _PLATFORM_DOMAINS

        domains = {platform: domain for domain, platform in _PLATFORM_DOMAINS.items()}
        for platform, domain in domains.items():
            kind = "audio" if download.is_audio_only_platform(platform) else "video"
            cache_key = "inline_audio" if kind == "audio" else download._INLINE_CACHE_KEY

            async def lookup(_hash, key):
                return {"file_id": "test-file", "file_size": 42} if key == cache_key else None

            for run in range(3):
                with self.subTest(platform=platform, run=run):
                    query = SimpleNamespace(query=f"https://{domain}/example",
                                            from_user=SimpleNamespace(id=2), answer=AsyncMock())
                    with patch.object(download, "_user_limit", return_value=100):
                        await download.handle_inline_tiktok(
                            query, SimpleNamespace(get_cached_file=lookup), Mock(),
                        )
                    self.assertEqual(query.answer.await_args.args[0][0].type, kind)

    async def test_cached_result_sends_video_and_does_not_schedule_download(self):
        query = SimpleNamespace(query=LINK + "?share=1", from_user=SimpleNamespace(id=2),
                                answer=AsyncMock())
        db = SimpleNamespace(get_cached_file=AsyncMock(return_value={
            "file_id": "video-id", "file_size": 42,
        }))
        with patch.object(download, "_user_limit", return_value=100), \
             patch.object(download, "_prepare_inline_tiktok") as prepare:
            await download.handle_inline_tiktok(query, db, Mock())
        result = query.answer.await_args.args[0][0]
        self.assertEqual(result.video_file_id, "video-id")
        prepare.assert_not_called()

    async def test_new_video_is_uploaded_once_then_appears_in_results(self):
        files = {}
        async def lookup(key, kind):
            return files.get((key, kind))

        async def save(key, kind, file_id, file_size):
            files[key, kind] = {"file_id": file_id, "file_size": file_size}

        @asynccontextmanager
        async def media(*args, **kwargs):
            yield SimpleNamespace(path=Path("video.mp4"), file_size=42,
                                  duration=5, width=320, height=480)

        @asynccontextmanager
        async def upload_slot(*args):
            yield

        async def send(build, timeout):
            return await build()

        bot = SimpleNamespace(send_video=AsyncMock(
            return_value=SimpleNamespace(video=SimpleNamespace(file_id="new-id")),
        ))
        query = SimpleNamespace(query=LINK, from_user=SimpleNamespace(id=2),
                                answer=AsyncMock(), bot=bot)
        db = SimpleNamespace(get_cached_file=lookup, store_cached_file=save)
        with patch.object(download, "_inline_jobs", {}), \
             patch.object(download, "_inline_cache_chat", return_value=-100123456), \
             patch.object(download, "_user_limit", return_value=100), \
             patch.object(download, "_download_limit", return_value=100), \
             patch.object(download, "ensure_public_url"), \
             patch.object(download, "expand_short_link", AsyncMock(return_value=LINK)), \
             patch.object(download, "download_video", return_value=object()) as load, \
             patch.object(download, "_admitted_media", media), \
             patch.object(download._upload_capacity, "slot", upload_slot), \
             patch.object(download, "_send_paced", send):
            await download.handle_inline_tiktok(query, db, Mock())
            placeholder = query.answer.await_args.args[0][0]
            self.assertEqual(placeholder.type, "article")
            task = download._inline_jobs[LINK][1]
            await download.handle_inline_tiktok(query, db, Mock())
            self.assertIs(download._inline_jobs[LINK][1], task)
            await task
            bot.edit_message_media = AsyncMock()
            result = SimpleNamespace(
                query=LINK, inline_message_id="inline-id", bot=bot,
                from_user=query.from_user,
            )
            await download.finish_inline_tiktok(result, db, Mock())
            await download.handle_inline_tiktok(query, db, Mock())
        bot.send_video.assert_awaited_once()
        self.assertIsNone(load.call_args.args[1])
        self.assertEqual(query.answer.await_args.args[0][0].video_file_id, "new-id")
        self.assertEqual(bot.edit_message_media.await_args.kwargs["media"].media, "new-id")

    def test_share_link_accepts_only_tiktok_video_or_short_link(self):
        self.assertEqual(
            download._inline_tiktok_link("https://\nvt.tiktok.com/ZSpQLDYac/"),
            "https://vt.tiktok.com/ZSpQLDYac/",
        )
        self.assertEqual(download._inline_tiktok_link(LINK + "?utm_source=share"), LINK)
        self.assertIsNone(download._inline_tiktok_link(LINK.replace("/video/", "/photo/")))
        self.assertIsNone(download._inline_tiktok_link(LINK.replace("www.tiktok.com", "www.tiktok.com.example.org")))
