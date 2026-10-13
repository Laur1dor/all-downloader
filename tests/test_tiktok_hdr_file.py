"""TikTok documents use the playable MP4 and cache their own Telegram IDs."""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message

from bot.downloader import Media
from bot.handlers import download as handler


class TikTokHdrFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_file_fetch_uses_playable_mp4_and_bypasses_old_hdr_document_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source-hdr.mp4"
            source.write_bytes(b"original HDR and original audio")
            preview = Path(directory) / "preview-sdr.mp4"
            preview.write_bytes(b"SDR preview")
            media = Media(preview, "test", preview.stat().st_size, 32, 960, 1920,
                          original_path=source)
            requests = []

            @asynccontextmanager
            async def fetch(*args, **kwargs):
                requests.append(kwargs)
                yield media

            @asynccontextmanager
            async def slot(*args):
                yield

            async def send(build, *args, **kwargs):
                return await build()

            async def cached(_url, key):
                if key == 'tiktok_document_source_v2':
                    return {'file_id': 'old-broken-hdr-document', 'file_size': 10}
                return None

            message = MagicMock(spec=Message)
            message.answer = AsyncMock(return_value=MagicMock(spec=Message))
            message.answer_document = AsyncMock(return_value=SimpleNamespace(
                document=SimpleNamespace(file_id='compatible-mp4-document'),
            ))
            callback = MagicMock(message=message)
            callback.data = 'ttfile:token'
            callback.from_user.id = 123
            callback.answer = AsyncMock()
            db = MagicMock(get_cached_file=AsyncMock(side_effect=cached),
                           store_cached_file=AsyncMock())
            settings = SimpleNamespace(admin_id=123, cookies_file=None)
            cache = MagicMock()
            cache.get.return_value = 'https://www.tiktok.com/@user/video/7696055072110677268'
            with (
                patch.object(handler, '_tiktok_original_path', return_value=None),
                patch.object(handler, 'download_video', fetch),
                patch.object(handler._upload_capacity, 'slot', slot),
                patch.object(handler, '_send_with_retries', side_effect=send),
                patch.object(handler, '_user_limit', return_value=1000),
                patch.object(handler, '_record', AsyncMock()),
                patch.object(handler, '_delete_silently', AsyncMock()),
            ):
                await handler.handle_tiktok_file_request(callback, db, settings, cache)
            self.assertFalse(requests[0].get('original_only', False))
            self.assertEqual(Path(message.answer_document.await_args.args[0].path), preview)
            self.assertEqual(db.store_cached_file.await_args.args[3], preview.stat().st_size)
            self.assertNotEqual(db.store_cached_file.await_args.args[1],
                                'tiktok_document_source_v2')

    async def test_repeat_document_uses_file_id_without_fetch_or_prepare(self):
        message = MagicMock(spec=Message)
        message.answer_document = AsyncMock(return_value=SimpleNamespace(
            document=SimpleNamespace(file_id='compatible-document'),
        ))
        callback = MagicMock(message=message)
        callback.data = 'ttfile:token'
        callback.from_user.id = 123
        callback.answer = AsyncMock()
        db = MagicMock(get_cached_file=AsyncMock(return_value={
            'file_id': 'compatible-document', 'file_size': 200,
        }), add_conversion=AsyncMock())
        cache = MagicMock()
        cache.get.return_value = 'https://www.tiktok.com/@user/video/7696055072110677268'

        async def send(build, *args, **kwargs):
            return await build()

        with (
            patch.object(handler, 'download_video') as fetch,
            patch.object(handler, '_tiktok_original_path') as retained,
            patch.object(handler, '_send_paced', side_effect=send),
            patch.object(handler, '_user_limit', return_value=1000),
        ):
            await handler.handle_tiktok_file_request(
                callback, db, SimpleNamespace(admin_id=123), cache,
            )
        fetch.assert_not_called()
        retained.assert_not_called()
        message.answer_document.assert_awaited_once()
        self.assertEqual(message.answer_document.await_args.args[0], 'compatible-document')
        self.assertEqual(db.get_cached_file.await_args.args[1],
                         handler._TIKTOK_DOCUMENT_CACHE_KEY)
        self.assertEqual(db.add_conversion.await_args.kwargs['processing_time'], 0.0)


if __name__ == '__main__':
    unittest.main()
