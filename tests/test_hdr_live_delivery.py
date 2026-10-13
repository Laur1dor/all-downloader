"""HDR metadata survives cached previews; Live Photo keeps its paired motion."""

import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message

from bot.downloader import AlbumMedia
from bot.handlers import download as handler
from bot.tiktok_live_photo import PreparedLivePhoto


class HdrLiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_sdr_preview_metadata_is_repaired_from_confirmed_hdr_file(self):
        url = 'https://www.tiktok.com/@u/video/7696055072110677268'
        preview = {'file_id': 'existing-preview', 'file_size': 100,
                   'description': handler._tiktok_video_cache_description('post text', False)}
        source = {'file_id': 'original-hdr', 'file_size': 200,
                  'description': '{"source_hdr":true,"structure_modified":false}'}

        async def cached(_hash, key):
            return source if key == handler._TIKTOK_DOCUMENT_CACHE_KEY else preview

        async def send(build, *_args):
            return await build()

        message = MagicMock(spec=Message, from_user=SimpleNamespace(id=123))
        message.answer_video = AsyncMock()
        db = MagicMock(get_cached_file=AsyncMock(side_effect=cached),
                       store_cached_file=AsyncMock(), add_conversion=AsyncMock())
        cache = MagicMock()
        cache.store.return_value = 'fixed-hdr-token'
        with (
            patch.object(handler, '_send_paced', side_effect=send),
            patch.object(handler, 'download_video') as fetch,
        ):
            self.assertTrue(await handler._send_cached_video(
                message, db, cache, url, 'tiktok', 1000,
                (handler._video_cache_key('tiktok'),),
            ))
        fetch.assert_not_called()
        self.assertEqual(message.answer_video.await_args.args[0], 'existing-preview')
        self.assertIn('Источник HDR', message.answer_video.await_args.kwargs['caption'])
        self.assertIn('нажмите «Файл»', message.answer_video.await_args.kwargs['caption'])
        self.assertEqual(db.store_cached_file.await_args.args[2], 'existing-preview')
        self.assertEqual(handler._tiktok_cached_video_metadata(
            db.store_cached_file.await_args.kwargs['description'],
        ), ('post text', True))
        self.assertEqual(source['file_id'], 'original-hdr')
        self.assertEqual(handler._album_descriptions.pop('fixed-hdr-token'), 'post text')

    async def test_sdr_file_and_missing_metadata_do_not_invent_hdr(self):
        for description in ('{"source_hdr":false}', 'broken JSON', None):
            with self.subTest(description=description):
                db = MagicMock(get_cached_file=AsyncMock(return_value={
                    'description': description,
                }))
                with patch('bot.tiktok_hdr.probe_tiktok_source_hdr', AsyncMock(return_value=None)):
                    self.assertFalse(await handler._tiktok_source_hdr_from_cache(
                        db, 'https://www.tiktok.com/@u/video/123', False,
                    ))

    async def test_fresh_sdr_preview_uses_proven_native_hdr_and_unknown_is_not_verified(self):
        db = MagicMock(get_cached_file=AsyncMock(return_value=None))
        for result in (True, False, None):
            with self.subTest(native_hdr=result), patch(
                'bot.tiktok_hdr.probe_tiktok_source_hdr', AsyncMock(return_value=result),
            ) as probe:
                hdr = await handler._tiktok_source_hdr_from_cache(
                    db, 'https://www.tiktok.com/@u/video/123', False,
                )
                self.assertIs(hdr, result)
                probe.assert_awaited_once()
                metadata = handler._tiktok_video_cache_description(
                    'text', hdr, source_hdr_checked=hdr is not None,
                )
                self.assertEqual(handler._tiktok_hdr_checked(metadata), hdr is not None)
                self.assertEqual('Источник HDR' in handler._video_caption(bool(hdr)), hdr is True)

    async def test_verified_sdr_cache_skips_native_probe(self):
        db = MagicMock(get_cached_file=AsyncMock())
        with patch('bot.tiktok_hdr.probe_tiktok_source_hdr', AsyncMock()) as probe:
            self.assertFalse(await handler._tiktok_source_hdr_from_cache(
                db, 'https://www.tiktok.com/@u/video/123', False, source_hdr_checked=True,
            ))
        db.get_cached_file.assert_not_awaited()
        probe.assert_not_awaited()

    async def test_run_link_reads_hdr_key_first_and_repairs_legacy_without_source_fetch(self):
        url = 'https://www.tiktok.com/@u/video/7696055072110677268'
        primary = handler._video_cache_key('tiktok')
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                preview = {'file_id': 'preview-id', 'file_size': 100,
                           'description': ('post text' if legacy else
                               handler._tiktok_video_cache_description(
                                   'post text', True, source_hdr_checked=True,
                               ))}

                async def cached(_hash, key, legacy_row=legacy, row=preview):
                    if key == handler._TIKTOK_DOCUMENT_CACHE_KEY:
                        return {'description': '{"source_hdr":true}', 'file_id': 'hdr-source'}
                    if key == ('video_validated' if legacy_row else primary):
                        return row
                    return None

                async def send(build, *_args):
                    return await build()

                message = MagicMock(spec=Message, text=url,
                                    from_user=SimpleNamespace(id=123, username=None))
                message.answer_video = AsyncMock()
                db = MagicMock(get_cached_file=AsyncMock(side_effect=cached),
                               store_cached_file=AsyncMock(), add_conversion=AsyncMock(),
                               upsert_user=AsyncMock())
                cache = MagicMock()
                cache.store.return_value = 'run-link-hdr-token'
                with (
                    patch.object(handler, 'expand_short_link', AsyncMock(return_value=url)),
                    patch.object(handler, 'ensure_public_url'),
                    patch.object(handler, '_user_limit', return_value=1000),
                    patch.object(handler, '_send_paced', side_effect=send),
                    patch.object(handler, 'download_video') as fetch,
                    patch('bot.tiktok_hdr.probe_tiktok_source_hdr', AsyncMock()) as probe,
                ):
                    await handler._run_link(
                        message, db, SimpleNamespace(admin_id=123), cache, MagicMock(),
                    )
                self.assertEqual(db.get_cached_file.await_args_list[0].args[1], primary)
                self.assertEqual(message.answer_video.await_args.args[0], 'preview-id')
                self.assertIn('Источник HDR', message.answer_video.await_args.kwargs['caption'])
                fetch.assert_not_called()
                probe.assert_not_awaited()
                if legacy:
                    db.store_cached_file.assert_awaited_once()
                    self.assertEqual(db.store_cached_file.await_args.args[1:3],
                                     (primary, 'preview-id'))
                else:
                    db.store_cached_file.assert_not_awaited()
                handler._album_descriptions.pop('run-link-hdr-token', None)

    async def test_cached_hdr_preview_retains_notice_and_plain_description(self):
        message = MagicMock(spec=Message, from_user=SimpleNamespace(id=123))
        message.answer_video = AsyncMock()
        db = MagicMock(get_cached_file=AsyncMock(return_value={
            'file_id': 'sdr-preview', 'file_size': 100,
            'description': handler._tiktok_video_cache_description(
                'post text', True, source_hdr_checked=True,
            ),
        }), add_conversion=AsyncMock())
        cache = MagicMock()
        cache.store.return_value = 'hdr-token'

        async def send(build, *_args):
            return await build()

        with patch.object(handler, '_send_paced', side_effect=send):
            sent = await handler._send_cached_video(
                message, db, cache, 'https://www.tiktok.com/@u/video/123', 'tiktok',
                1000, (handler._video_cache_key('tiktok'),),
            )
        self.assertTrue(sent)
        self.assertIn('Источник HDR', message.answer_video.await_args.kwargs['caption'])
        self.assertIn('нажмите «Файл»', message.answer_video.await_args.kwargs['caption'])
        self.assertEqual(handler._album_descriptions.pop('hdr-token'), 'post text')
        self.assertNotIn('HDR', handler._video_caption(False))

    async def _deliver(self, mixed):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            still = root / 'still.jpg'
            motion = root / 'motion.mp4'
            static = root / 'static.jpg'
            for path in (still, motion, static):
                path.write_bytes(b'test bytes')
            pair = PreparedLivePhoto(still, motion, still, motion, 3.0, 320, 240)
            album = AlbumMedia([static, still] if mixed else [still], None,
                               live_photos={1 if mixed else 0: pair})

            @asynccontextmanager
            async def fetch(*_args, **_kwargs):
                yield album

            @asynccontextmanager
            async def slot(*_args):
                yield

            async def send(build, *_args):
                return await build()

            message = MagicMock(spec=Message, chat=SimpleNamespace(id=123),
                                message_thread_id=None)
            message.answer = AsyncMock()
            message.answer_media_group = AsyncMock()
            delivered = []

            async def send_static(*args, **kwargs):
                delivered.append('photo')

            async def send_live(*args, **kwargs):
                delivered.append('live')

            message.answer_photo = AsyncMock(side_effect=send_static)
            message.bot.send_live_photo = AsyncMock(side_effect=send_live)
            cache = MagicMock()
            cache.store.return_value = 'token'
            with (
                patch.object(handler, 'download_album', fetch),
                patch.object(handler._upload_capacity, 'slot', slot),
                patch.object(handler, '_send_paced', side_effect=send),
                patch.object(handler, '_record', AsyncMock()),
                patch.object(handler, '_delete_silently', AsyncMock()),
                patch.object(handler, 'photo_needs_document', return_value=False),
            ):
                self.assertTrue(await handler._deliver_album(
                    message, MagicMock(), MagicMock(spec=Message),
                    'https://www.tiktok.com/@u/photo/123', MagicMock(cookies_file=None),
                    'tiktok', 123, time.monotonic(), cache,
                ))
            if mixed:
                self.assertEqual(delivered, ['photo', 'live'])
                message.answer_media_group.assert_not_called()
                call = message.bot.send_live_photo.await_args.kwargs
                self.assertEqual(Path(call['live_photo'].path), motion)
                self.assertEqual(Path(call['photo'].path), still)
            else:
                message.bot.send_live_photo.assert_awaited_once()
                call = message.bot.send_live_photo.await_args.kwargs
                self.assertEqual(Path(call['live_photo'].path), motion)
                self.assertEqual(Path(call['photo'].path), still)
                message.answer_media_group.assert_not_called()

    async def test_single_live_photo_uses_native_method(self):
        await self._deliver(False)

    async def test_mixed_album_preserves_native_pair_and_slide_order(self):
        await self._deliver(True)


if __name__ == '__main__':
    unittest.main()
