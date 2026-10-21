"""Large media calls must pass their deadline through aiogram's session."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError
from aiogram.enums import ChatType
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, FSInputFile, Message

from bot.handlers.download import DeliveryUnknownError, _UploadCapacity, _send_paced, _send_with_retries


class CaptureSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.default_timeout = 90
        self.seen_timeout: int | None = None

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None,
    ) -> Any:
        self.seen_timeout = timeout if timeout is not None else self.default_timeout
        return "sent"

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, **kwargs: Any):
        yield b""


class MediaUploadTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_bound_send_video_uses_per_file_timeout(self) -> None:
        session = CaptureSession()
        bot = Bot("123:ABC", session=session)
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=123, type=ChatType.PRIVATE),
        ).as_(bot)

        result = await _send_paced(
            lambda: message.answer_video(FSInputFile("/tmp/video.mp4")), 480,
        )

        self.assertEqual(result, "sent")
        self.assertEqual(session.seen_timeout, 480)
        await bot.session.close()

    async def test_coroutine_callers_still_work(self) -> None:
        async def send() -> str:
            return "sent"

        self.assertEqual(await _send_paced(send, 10), "sent")


class DeliveryConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_server_gets_confirmation_budget_after_small_file_deadline(self):
        session = CaptureSession()
        session.api = TelegramAPIServer.from_base("http://local-api:8081")
        bot = Bot("123:ABC", session=session)
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=123, type=ChatType.PRIVATE)).as_(bot)
        await _send_paced(lambda: message.answer_video("file_id"), 60)
        self.assertEqual(session.seen_timeout, 600)
        await bot.session.close()

    async def test_late_response_is_awaited_once_and_counted_as_confirmed(self):
        session = CaptureSession()
        session.api = TelegramAPIServer.from_base("http://local-api:8081")
        bot = Bot("123:ABC", session=session)
        calls = 0

        async def late_response(_bot, _method, timeout=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.04)
            return "accepted"

        session.make_request = late_response
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=123, type=ChatType.PRIVATE)).as_(bot)
        db = SimpleNamespace(record_upload=AsyncMock())
        capacity = _UploadCapacity()
        with patch("bot.handlers.download._LOCAL_CONFIRM_SECONDS", 0.2):
            async with capacity.slot(1024, False, db, "tiktok"):
                result = await _send_with_retries(
                    lambda: message.answer_video("file_id"), bot, 0.01,
                )
        self.assertEqual(result, "accepted")
        self.assertEqual(calls, 1)
        db.record_upload.assert_awaited_once()
        self.assertEqual(capacity._small, 0)
        await bot.session.close()

    async def test_lost_response_is_unknown_without_retry_or_success_sample(self):
        attempts = 0

        async def lost_response():
            nonlocal attempts
            attempts += 1
            raise TelegramNetworkError(method=None, message="Disconnected after body")

        db = SimpleNamespace(record_upload=AsyncMock())
        capacity = _UploadCapacity()
        with self.assertRaises(DeliveryUnknownError):
            async with capacity.slot(1024, False, db, "tiktok"):
                await _send_with_retries(lost_response, None, 1, delay=0)
        self.assertEqual(attempts, 1)
        db.record_upload.assert_not_awaited()
        self.assertEqual(capacity._small, 0)

    async def test_hard_deadline_is_unknown_and_releases_upload_capacity(self):
        async def stalled():
            await asyncio.sleep(10)

        db = SimpleNamespace(record_upload=AsyncMock())
        capacity = _UploadCapacity()
        with patch("bot.handlers.download._OUTBOUND_QUEUE_SECONDS", 0), self.assertRaises(DeliveryUnknownError):
            async with capacity.slot(1024, False, db, "youtube"):
                await _send_paced(stalled, 0.01)
        db.record_upload.assert_not_awaited()
        self.assertEqual(capacity._small, 0)

    async def test_cancellation_does_not_count_as_confirmed_upload(self):
        db = SimpleNamespace(record_upload=AsyncMock())
        capacity = _UploadCapacity()
        with self.assertRaises(asyncio.CancelledError):
            async with capacity.slot(1024, False, db, "youtube"):
                raise asyncio.CancelledError
        db.record_upload.assert_not_awaited()
        self.assertEqual(capacity._small, 0)


class CachedDeliveryOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_cache_send_keeps_file_and_records_unknown_even_if_notice_fails(self):
        from unittest.mock import MagicMock
        from aiogram.types import Message
        from bot.handlers import download
        from bot.urlcache import UrlCache

        message = MagicMock(spec=Message, from_user=SimpleNamespace(id=123))
        message.answer_video = AsyncMock(side_effect=DeliveryUnknownError("lost response"))
        message.answer = AsyncMock(side_effect=TelegramNetworkError(None, "notice lost"))
        db = SimpleNamespace(
            get_cached_file=AsyncMock(return_value={"file_id": "saved", "file_size": 100,
                                                    "description": None}),
            delete_cached_file=AsyncMock(), add_conversion=AsyncMock(),
        )
        with patch.object(download, "_send_paced", side_effect=DeliveryUnknownError("lost")):
            handled = await download._send_cached_video(
                message, db, UrlCache(), "https://x.com/example/status/123", "twitter",
                1000, ("video_mp4_v1",),
            )
        self.assertTrue(handled)
        db.delete_cached_file.assert_not_awaited()
        self.assertEqual(db.add_conversion.await_args.args[1], "delivery_unknown")

    async def test_known_connection_failure_is_retried_without_ambiguous_duplicates(self):
        import aiohttp
        from aiogram.methods import SendMessage
        from bot.handlers import download

        failed = TelegramNetworkError(SendMessage(chat_id=1, text="x"), "connect refused")
        failed.__cause__ = aiohttp.ClientConnectorError(
            SimpleNamespace(ssl=False, host="local-api", port=8081), OSError("refused"),
        )
        with patch.object(download, "_send_paced", AsyncMock(side_effect=[failed, "accepted"])) as send:
            self.assertEqual(await download._send_with_retries(lambda: None, None, 1, delay=0),
                             "accepted")
        self.assertEqual(send.await_count, 2)
