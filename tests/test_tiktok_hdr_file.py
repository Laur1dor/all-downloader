"""Cold TikTok File requests deliver the source rather than the SDR preview."""

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
    async def test_cold_file_fetch_preserves_hdr_and_avoids_old_preview_document_cache(self):
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
                if key == 'tiktok_document':
                    return {'file_id': 'old-sdr-document', 'file_size': 10}
                return None

            message = MagicMock(spec=Message)
            message.answer = AsyncMock(return_value=MagicMock(spec=Message))
            message.answer_document = AsyncMock(return_value=SimpleNamespace(
                document=SimpleNamespace(file_id='original-hdr-document'),
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
            self.assertTrue(requests[0]['original_only'])
            self.assertEqual(Path(message.answer_document.await_args.args[0].path), source)
            self.assertEqual(db.store_cached_file.await_args.args[3], source.stat().st_size)
            self.assertNotEqual(db.store_cached_file.await_args.args[1], 'tiktok_document')


if __name__ == '__main__':
    unittest.main()
