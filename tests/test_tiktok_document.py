"""TikTok source button must send actual Telegram documents."""

import json
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendDocument
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
            for index, photo in enumerate(photos, 1):
                photo.write_bytes(b"j" * index)
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
                side_effect=[MagicMock(document=MagicMock(file_id=f"doc-id-{i}"))
                             for i in range(1, 32)],
            )
            callback = MagicMock()
            callback.message = message
            callback.data = "ttfile:token"
            callback.from_user.id = 123
            callback.answer = AsyncMock()
            db = MagicMock()
            db.get_cached_file = AsyncMock(return_value=None)
            db.store_cached_file = AsyncMock()
            db.add_conversion = AsyncMock()
            settings = MagicMock(admin_id=123, cookies_file=None)
            cache = MagicMock()
            cache.get.return_value = url

            with (
                patch.object(handler, "download_album", side_effect=fake_album) as download,
                patch.object(handler._upload_capacity, "slot", fake_slot),
                patch.object(handler, "_send_paced", side_effect=run_send),
                patch.object(handler, "_record", new_callable=AsyncMock) as record,
                patch.object(handler, "_delete_silently", new_callable=AsyncMock),
                patch.object(handler, "_user_limit", return_value=10_000),
            ):
                await handler.handle_tiktok_file_request(callback, db, settings, cache)

                self.assertEqual(message.answer_document.await_count, 31)
                db.store_cached_file.assert_awaited_once()
                saved = db.store_cached_file.await_args
                self.assertEqual(saved.args[1], "tiktok_photo_documents_v1")
                self.assertEqual(saved.args[2:4], ("doc-id-1", sum(range(1, 32))))
                manifest = json.loads(saved.kwargs["description"])
                self.assertEqual(manifest, [
                    {"file_id": f"doc-id-{i}", "file_size": i} for i in range(1, 32)
                ])
                self.assertEqual(record.await_args.args[2], handler.STATUS_DONE)

                # A repeated request uses every cached document ID, without
                # fetching the source or uploading local files again.
                db.get_cached_file.return_value = {"description": saved.kwargs["description"]}
                message.answer_document.reset_mock()
                message.answer_document.side_effect = None
                message.answer_document.return_value = MagicMock(document=MagicMock(file_id="sent"))
                await handler.handle_tiktok_file_request(callback, db, settings, cache)
                download.assert_called_once()
                self.assertEqual(
                    [call.args[0] for call in message.answer_document.await_args_list],
                    [f"doc-id-{i}" for i in range(1, 32)],
                )
                self.assertEqual(db.add_conversion.await_args.args[1], handler.STATUS_DONE)
                self.assertEqual(
                    db.add_conversion.await_args.kwargs["file_size"], sum(range(1, 32)),
                )

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
            db.get_cached_file.return_value = None
            db.store_cached_file.reset_mock()
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
            db.store_cached_file.assert_not_awaited()

    async def test_invalid_photo_manifest_is_evicted_and_downloaded_again(self):
        for manifest in ("not JSON", '{"file_id":"doc"}',
                         '[{"file_id":"doc","file_size":true}]'):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as tmpdir:
                photo = Path(tmpdir) / "photo.jpg"
                photo.write_bytes(b"jpeg")

                @asynccontextmanager
                async def album(*_args, source=photo, **_kwargs):
                    yield AlbumMedia(items=[source], music=None)

                @asynccontextmanager
                async def slot(*_args, **_kwargs):
                    yield

                async def send(build, _timeout, **_kwargs):
                    return await build()

                message = MagicMock(spec=Message)
                message.answer = AsyncMock(return_value=MagicMock(spec=Message))
                message.answer_document = AsyncMock(
                    return_value=MagicMock(document=MagicMock(file_id="fresh-id")),
                )
                callback = MagicMock()
                callback.message = message
                callback.data = "ttfile:token"
                callback.from_user.id = 123
                callback.answer = AsyncMock()
                db = MagicMock()
                db.get_cached_file = AsyncMock(return_value={"description": manifest})
                db.delete_cached_file = AsyncMock()
                db.store_cached_file = AsyncMock()
                settings = MagicMock(admin_id=123, cookies_file=None)
                cache = MagicMock()
                cache.get.return_value = "https://www.tiktok.com/@example/photo/123"
                with (
                    patch.object(handler, "download_album", side_effect=album) as download,
                    patch.object(handler._upload_capacity, "slot", slot),
                    patch.object(handler, "_send_paced", side_effect=send),
                    patch.object(handler, "_record", new_callable=AsyncMock) as record,
                    patch.object(handler, "_delete_silently", new_callable=AsyncMock),
                    patch.object(handler, "_user_limit", return_value=10_000),
                ):
                    await handler.handle_tiktok_file_request(callback, db, settings, cache)
                db.delete_cached_file.assert_awaited_once_with(
                    handler.hash_url(cache.get.return_value), "tiktok_photo_documents_v1",
                )
                download.assert_called_once()
                self.assertEqual(record.await_args.args[2], handler.STATUS_DONE)
                self.assertEqual(json.loads(db.store_cached_file.await_args.kwargs["description"]),
                                 [{"file_id": "fresh-id", "file_size": 4}])

    async def test_invalid_cached_file_after_partial_delivery_does_not_repeat_photos(self):
        message = MagicMock(spec=Message)
        message.answer = AsyncMock()
        message.answer_document = AsyncMock(side_effect=[
            MagicMock(document=MagicMock(file_id="first")),
            TelegramBadRequest(
                method=SendDocument(chat_id=123, document="expired"),
                message="wrong file identifier",
            ),
        ])
        callback = MagicMock()
        callback.message = message
        callback.data = "ttfile:token"
        callback.from_user.id = 123
        callback.answer = AsyncMock()
        db = MagicMock()
        db.get_cached_file = AsyncMock(return_value={"description": json.dumps([
            {"file_id": "first", "file_size": 4},
            {"file_id": "expired", "file_size": 5},
        ])})
        db.delete_cached_file = AsyncMock()
        db.add_conversion = AsyncMock()
        settings = MagicMock(admin_id=123, cookies_file=None)
        cache = MagicMock()
        cache.get.return_value = "https://www.tiktok.com/@example/photo/123"

        async def send(build, _timeout, **_kwargs):
            return await build()

        with (
            patch.object(handler, "download_album") as download,
            patch.object(handler, "_send_paced", side_effect=send),
            patch.object(handler, "_user_limit", return_value=10_000),
        ):
            await handler.handle_tiktok_file_request(callback, db, settings, cache)
        db.delete_cached_file.assert_awaited_once_with(
            handler.hash_url(cache.get.return_value), "tiktok_photo_documents_v1",
        )
        download.assert_not_called()
        self.assertEqual(message.answer_document.await_count, 2)
        self.assertEqual(db.add_conversion.await_args.args[1], handler.STATUS_FAILED)
        self.assertIn("сохранённых файлов недоступен", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
