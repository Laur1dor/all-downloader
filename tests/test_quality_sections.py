"""Quality selection for remote variants and locally prepared excerpts."""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.downloader import DownloadFailedError, QualityOption
from bot.handlers import download
from bot.urlcache import UrlCache


class ExcerptQualityTests(unittest.IsolatedAsyncioTestCase):
    async def choose(self, url, options, limit, *, local=False, height=None):
        edits = []

        async def edit(_message, text, **kwargs):
            edits.append((text, kwargs))
            if height is not None and "reply_markup" in kwargs:
                event, state = download._pending_choices["quality-test"]
                state["height"] = height
                event.set()

        with (
            patch.object(download, "_safe_edit", side_effect=edit),
            patch.object(
                download, "probe_quality_options", AsyncMock(return_value=options),
            ) as probe,
            patch.object(download, "_MEDIA_CHOICE_TIMEOUT", 0.01),
        ):
            result = await download._select_quality(
                MagicMock(), "quality-test", url, SimpleNamespace(cookies_file=None), limit,
                time_range=(30, 39), **({"options": options} if local else {}),
            )
        self.assertNotIn("quality-test", download._pending_choices)
        if local:
            probe.assert_not_awaited()
        return result, edits

    async def test_youtube_excerpt_is_not_locked_by_whole_source_size(self):
        selected, edits = await self.choose(
            "https://www.youtube.com/watch?v=test", [QualityOption(1080, 9 * 1024**3)], 100,
        )
        self.assertEqual(selected.height, 1080)
        prompt = next(item for item in edits if "Выберите качество" in item[0])
        self.assertIn("только выбранный интервал", prompt[0])
        button = prompt[1]["reply_markup"].inline_keyboard[0][0].text
        self.assertIn("1080p", button)
        self.assertIn("~9216 МБ", button)
        self.assertNotIn("🔒", button)
        self.assertIn("полного исходника", prompt[0])

    async def test_rule34video_menu_labels_whole_source_sizes_and_respects_limit(self):
        selected, edits = await self.choose(
            "https://rule34video.com/video/123/example/",
            [QualityOption(1080, 200), QualityOption(480, 50)], 100,
        )
        self.assertEqual(selected.height, 480)
        prompt = next(item for item in edits if "Выберите качество" in item[0])
        self.assertIn("полного исходника", prompt[0])
        self.assertIn("🔒", prompt[1]["reply_markup"].inline_keyboard[0][0].text)

    async def test_manual_choice_is_retained_for_remote_source(self):
        option = QualityOption(480, 50, "mp4-480")
        selected, _ = await self.choose(
            "https://rule34video.com/video/123/example/",
            [QualityOption(1080, 80), option], 100, height=480,
        )
        self.assertIs(selected, option)

    async def test_unavailable_rule34video_metadata_requires_retry(self):
        with self.assertRaisesRegex(DownloadFailedError, 'доступные качества'):
            await self.choose('https://rule34video.com/video/123/example/', [], 100)

    async def test_single_source_offers_only_original_without_probe(self):
        options = download._source_quality_options(1080, 50)
        self.assertEqual([option.height for option in options], [1080])
        selected, edits = await self.choose(
            "https://rule34.xxx/index.php?page=post&s=view&id=123",
            options, 100, local=True, height=1080,
        )
        self.assertEqual(selected.height, 1080)
        self.assertIn("Использую исходник", edits[-1][0])

    def test_single_source_keeps_nonstandard_resolution(self):
        self.assertEqual([o.height for o in download._source_quality_options(358, 50)], [358])
        self.assertEqual(download._source_quality_options(None, 50), [])

    async def test_oversized_local_source_cannot_be_selected_as_remote_variant(self):
        with self.assertRaises(DownloadFailedError):
            await self.choose(
                "https://rule34video.com/video/123/example/",
                [QualityOption(1080, 200)], 100, height=1080,
            )

    async def test_rule34_full_video_offers_source_quality_without_caption(self):
        @asynccontextmanager
        async def slot(*args):
            yield

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.mp4'
            source.write_bytes(b'video')
            message = MagicMock(answer_video=AsyncMock())
            db = MagicMock(store_cached_file=AsyncMock())
            sent = SimpleNamespace(video=SimpleNamespace(file_id='source'), document=None)

            async def send(callback, *args):
                await callback()
                return sent

            with (
                patch.object(download, 'video_metadata', return_value=(1920, 1080, 30)),
                patch.object(download, '_select_quality', AsyncMock()) as choose,
                patch.object(download, '_download_limit', return_value=100),
                patch.object(download, '_upload_capacity', SimpleNamespace(slot=slot)),
                patch.object(download, '_send_with_retries', side_effect=send),
                patch.object(download, '_record', AsyncMock()),
                patch.object(download, '_delete_silently', AsyncMock()),
            ):
                result = await download._deliver_single_video(
                    message, db, MagicMock(),
                    'https://rule34.xxx/index.php?page=post&s=view&id=123',
                    SimpleNamespace(items=[source], description=''), 'rule34',
                    1, 0, UrlCache(), True, SimpleNamespace(cookies_file=None),
                )
            self.assertTrue(result)
            choose.assert_awaited_once()
            self.assertEqual([o.height for o in choose.await_args.kwargs['options']], [1080])
            self.assertNotIn('Качество:', message.answer_video.await_args.kwargs['caption'])
