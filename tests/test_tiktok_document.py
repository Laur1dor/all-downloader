"""TikTok source button must send actual Telegram documents."""

import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message

from bot.downloader import AlbumMedia
from bot.handlers import download as handler


class TikTokDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_31_photo_post_splits_telegram_groups(self):
        url = "https://www.tiktok.com/@rastro.incomum/photo/7690633438700866834"
        with tempfile.TemporaryDirectory() as tmpdir:
            photos = [Path(tmpdir) / f"photo-{i}.jpg" for i in range(1, 32)]
            for photo in photos:
                photo.write_bytes(b"jpeg")
            album = AlbumMedia(items=photos, music=None)

            @asynccontextmanager
            async def fake_album(*_args, **_kwargs):
                yield album

            @asynccontextmanager
            async def fake_slot(*_args, **_kwargs):
                yield

            async def run_send(build, _timeout, **_kwargs):
                return await build()

            message = MagicMock(spec=Message)
            message.answer_media_group = AsyncMock()
            message.answer_photo = AsyncMock()
            message.answer = AsyncMock()
            status = MagicMock(spec=Message)
            settings = MagicMock(admin_id=123, cookies_file=None)
            cache = MagicMock()
            cache.store.return_value = "token"

            with (
                patch.object(handler, "download_album", fake_album),
                patch.object(handler._upload_capacity, "slot", fake_slot),
                patch.object(handler, "_send_paced", side_effect=run_send),
                patch.object(handler, "_record", new_callable=AsyncMock),
                patch.object(handler, "_delete_silently", new_callable=AsyncMock),
                patch.object(handler, "photo_needs_document", return_value=False),
            ):
                sent = await handler._deliver_album(
                    message, MagicMock(), status, url, settings, "tiktok", 123,
                    time.monotonic(), cache,
                )
            self.assertTrue(sent)
            self.assertEqual(message.answer_media_group.await_count, 3)
            self.assertEqual(
                [len(call.args[0]) for call in message.answer_media_group.await_args_list],
                [10, 10, 10],
            )
            message.answer_photo.assert_awaited_once()

    async def test_photo_button_uses_documents_and_checks_telegram_result(self):
        url = "https://www.tiktok.com/@rastro.incomum/photo/7690633438700866834"
        with tempfile.TemporaryDirectory() as tmpdir:
            photos = [Path(tmpdir) / f"photo-{i}.jpg" for i in range(1, 32)]
            for photo in photos:
                photo.write_bytes(b"jpeg")
            album = AlbumMedia(items=photos, music=None)

            @asynccontextmanager
            async def fake_album(*_args, **_kwargs):
                yield album

            @asynccontextmanager
            async def fake_slot(*_args, **_kwargs):
                yield

            async def run_send(build, _timeout, **_kwargs):
                return await build()

            message = MagicMock(spec=Message)
            message.answer = AsyncMock(return_value=MagicMock(spec=Message))
            message.answer_document = AsyncMock(
                return_value=MagicMock(document=MagicMock(file_id="doc-id")),
            )
            callback = MagicMock()
            callback.message = message
            callback.data = "ttfile:token"
            callback.from_user.id = 123
            callback.answer = AsyncMock()
            db = MagicMock()
            db.get_cached_file = AsyncMock(return_value=None)
            settings = MagicMock(admin_id=123, cookies_file=None)
            cache = MagicMock()
            cache.get.return_value = url

            with (
                patch.object(handler, "download_album", fake_album),
                patch.object(handler._upload_capacity, "slot", fake_slot),
                patch.object(handler, "_send_paced", side_effect=run_send),
                patch.object(handler, "_record", new_callable=AsyncMock) as record,
                patch.object(handler, "_delete_silently", new_callable=AsyncMock),
                patch.object(handler, "_user_limit", return_value=10_000),
            ):
                await handler.handle_tiktok_file_request(callback, db, settings, cache)

            self.assertEqual(message.answer_document.await_count, 31)
            self.assertTrue(all(
                call.kwargs["disable_content_type_detection"] is True
                for call in message.answer_document.await_args_list
            ))
            self.assertEqual(record.await_args.args[2], handler.STATUS_DONE)

            # A local Bot API may still classify MP4 as video. This must be
            # reported as failure rather than cached as a source document.
            message.answer_document.reset_mock()
            message.answer_document.return_value = MagicMock(document=None)
            with (
                patch.object(handler, "download_album", fake_album),
                patch.object(handler._upload_capacity, "slot", fake_slot),
                patch.object(handler, "_send_paced", side_effect=run_send),
                patch.object(handler, "_record", new_callable=AsyncMock) as failed_record,
                patch.object(handler, "_safe_edit", new_callable=AsyncMock) as edit,
                patch.object(handler, "_user_limit", return_value=10_000),
            ):
                await handler.handle_tiktok_file_request(callback, db, settings, cache)
            self.assertEqual(message.answer_document.await_count, 1)
            self.assertEqual(failed_record.await_args.args[2], handler.STATUS_FAILED)
            self.assertIn("не принял фото как файл", edit.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
