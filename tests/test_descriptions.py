"""Descriptions survive cached media and Telegram's message-size limits."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Chat, Message

from bot import downloader
from bot.handlers import download as handler
from bot.urlcache import UrlCache


class DescriptionDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.url = "https://www.tiktok.com/@example/video/123456789"
        self.cache = UrlCache()
        self.token = self.cache.store(self.url)
        self.callback = SimpleNamespace(
            data="desc:" + self.token, answer=AsyncMock(),
            message=Message(message_id=1, date=datetime.now(timezone.utc),
                            chat=Chat(id=1, type="private")),
        )
        self.settings = SimpleNamespace(cookies_file=None)
        self.db = MagicMock(get_cached_file=AsyncMock(return_value=None))

    def tearDown(self):
        handler._album_descriptions.pop(self.token, None)

    async def test_large_unicode_description_is_complete_and_never_refetched(self):
        text = ("Описание <текст> & 😀\n" * 600) + "конец"
        handler._remember_description(self.token, text)
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch.object(handler, "fetch_description", new=AsyncMock()) as fetch,
        ):
            await handler.handle_description_request(
                self.callback, self.settings, self.cache, self.db,
            )
        chunks = [call.args[0] for call in answer.await_args_list]
        self.assertGreater(len(chunks), 2)
        self.assertEqual("".join(chunks).removeprefix("📝 Описание:\n\n"), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks))
        self.assertTrue(all(call.kwargs["parse_mode"] is None for call in answer.await_args_list))
        fetch.assert_not_awaited()
        self.db.get_cached_file.assert_not_awaited()

    async def test_existing_video_button_reads_full_caption_from_database(self):
        text = "старое описание\n" * 700
        self.db.get_cached_file.return_value = {
            "description": handler._tiktok_video_cache_description(text, None),
        }
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch.object(handler, "fetch_description", new=AsyncMock()) as fetch,
        ):
            await handler.handle_description_request(
                self.callback, self.settings, self.cache, self.db,
            )
        self.assertEqual(
            "".join(call.args[0] for call in answer.await_args_list)
            .removeprefix("📝 Описание:\n\n"), text.strip(),
        )
        self.assertEqual(handler._album_descriptions[self.token], text.strip())
        fetch.assert_not_awaited()

    async def test_network_failure_is_not_reported_as_missing_description(self):
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch.object(handler, "fetch_description", new=AsyncMock(
                side_effect=downloader.DownloadFailedError("blocked"))),
        ):
            await handler.handle_description_request(
                self.callback, self.settings, self.cache, self.db,
            )
        self.assertIn("Не удалось получить", answer.await_args.args[0])
        self.assertNotIn("Описания нет", answer.await_args.args[0])

    async def test_valid_post_without_caption_reports_no_description(self):
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch.object(handler, "fetch_description", new=AsyncMock(return_value=None)),
        ):
            await handler.handle_description_request(
                self.callback, self.settings, self.cache, self.db,
            )
        self.assertIn("Описания нет", answer.await_args.args[0])

    async def test_refetched_description_is_remembered_for_next_click(self):
        with (
            patch.object(Message, "answer", new=AsyncMock()),
            patch.object(handler, "fetch_description", new=AsyncMock(return_value="text")) as fetch,
        ):
            for _ in range(2):
                await handler.handle_description_request(
                    self.callback, self.settings, self.cache, self.db,
                )
        fetch.assert_awaited_once()


class DescriptionResolverTests(unittest.TestCase):
    def test_tiktok_native_caption_does_not_use_video_extractor(self):
        session = MagicMock()
        text = "длинное описание" * 500
        with (
            patch.object(downloader, "_tiktok_item", return_value=({"desc": text}, session)),
            patch.object(downloader, "_tiktok_cookies", return_value={}),
            patch.object(downloader, "_extract_info") as extract,
        ):
            self.assertEqual(downloader._fetch_tiktok_description_sync("url", None), text)
        session.close.assert_called_once()
        extract.assert_not_called()

    def test_blocked_native_metadata_uses_anonymous_resolver(self):
        session = MagicMock()
        with (
            patch.object(downloader, "_tiktok_item", side_effect=
                         downloader.DownloadFailedError("blocked")),
            patch.object(downloader, "_tiktok_cookies", return_value={}),
            patch.object(downloader, "_tikwm_data", return_value=({"title": "caption"}, session)),
        ):
            self.assertEqual(downloader._fetch_tiktok_description_sync("url", None), "caption")
        session.close.assert_called_once()

    def test_chunks_preserve_exact_text_at_unicode_boundaries(self):
        for text in ("a" * 4000, "a" * 4001, "😀" * 2001, "a" * 3999 + "😀end"):
            with self.subTest(length=len(text)):
                chunks = handler._description_chunks(text)
                self.assertEqual("".join(chunks), text)
                self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 4000
                                    for chunk in chunks))
