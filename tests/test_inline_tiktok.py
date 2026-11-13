import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import download


LINK = "https://www.tiktok.com/@example/video/7412345678901234567"


class InlineTikTokTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(download._inline_tiktok_link(LINK + "?utm_source=share"), LINK)
        self.assertIsNone(download._inline_tiktok_link(LINK.replace("/video/", "/photo/")))
        self.assertIsNone(download._inline_tiktok_link(LINK.replace("www.tiktok.com", "www.tiktok.com.example.org")))
