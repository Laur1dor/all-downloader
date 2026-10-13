"""Flood waits and scheduling of outgoing Bot API calls."""

from __future__ import annotations

import asyncio
import time
import unittest

from aiogram.exceptions import TelegramRetryAfter

from bot.telegram_outbound import OutgoingLimiter, bulk_call, progress_call


class Method:
    def __init__(self, name: str, media: list | None = None,
                 chat_id: int | None = None) -> None:
        self.__api_method__ = name
        self.media = media or []
        self.chat_id = chat_id


class OutgoingLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_is_shared_and_retried_once(self) -> None:
        limiter = OutgoingLimiter(media_spacing=0.5)
        media = Method("sendVideo")
        reply = Method("sendMessage")
        first_seen = asyncio.Event()
        calls: list[str] = []

        async def request(_bot, method):
            calls.append(method.__api_method__)
            if method is media and calls.count("sendVideo") == 1:
                first_seen.set()
                raise TelegramRetryAfter(method, "flood", retry_after=0.2)
            return method.__api_method__

        media_task = asyncio.create_task(limiter(request, None, media))
        await asyncio.wait_for(first_seen.wait(), 1)
        start = time.monotonic()
        reply_task = asyncio.create_task(limiter(request, None, reply))
        self.assertEqual(await asyncio.wait_for(reply_task, 2), "sendMessage")
        self.assertGreaterEqual(time.monotonic() - start, 0.15)
        self.assertEqual(await asyncio.wait_for(media_task, 2), "sendVideo")
        self.assertEqual(calls, ["sendVideo", "sendMessage", "sendVideo"])

    async def test_interactive_reply_precedes_bulk_and_progress(self) -> None:
        limiter = OutgoingLimiter(media_spacing=0.5)
        limiter._cooldown_until = time.monotonic() + 0.1
        order: list[str] = []

        async def request(_bot, method):
            order.append(method.__api_method__)

        media = asyncio.create_task(limiter(request, None, Method("sendPhoto")))
        with progress_call():
            progress = asyncio.create_task(limiter(request, None, Method("editMessageText")))
        reply = asyncio.create_task(limiter(request, None, Method("sendMessage")))
        await asyncio.wait_for(asyncio.gather(media, progress, reply), 2)
        self.assertEqual(order, ["sendMessage", "sendPhoto", "editMessageText"])

    async def test_queue_timeout_releases_waiter(self) -> None:
        limiter = OutgoingLimiter(media_spacing=0.5)
        limiter._cooldown_until = time.monotonic() + 60
        from unittest.mock import patch

        with patch("bot.telegram_outbound._MAX_WAIT", 0.05), self.assertRaises(TelegramRetryAfter):
            await limiter(lambda *_: None, None, Method("sendMessage"))
        self.assertEqual(limiter._queue, [])

    async def test_bulk_follows_media_and_precedes_progress(self) -> None:
        limiter = OutgoingLimiter(media_spacing=0.5)
        limiter._cooldown_until = time.monotonic() + 0.1
        order: list[str] = []

        async def request(_bot, method):
            order.append(method.__api_method__)

        with progress_call():
            progress = asyncio.create_task(limiter(request, None, Method("editMessageText")))
        with bulk_call():
            bulk = asyncio.create_task(limiter(request, None, Method("copyMessage")))
        media = asyncio.create_task(limiter(request, None, Method("sendVideo")))
        await asyncio.wait_for(asyncio.gather(progress, bulk, media), 2)
        self.assertEqual(order, ["sendVideo", "copyMessage", "editMessageText"])

    async def test_private_chat_sends_are_spaced(self) -> None:
        limiter = OutgoingLimiter(media_spacing=0.5)

        async def request(_bot, _method):
            return None

        await limiter(request, None, Method("sendMessage", chat_id=123))
        started = time.monotonic()
        await limiter(request, None, Method("sendMessage", chat_id=123))
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        await limiter(request, None, Method("sendMessage", chat_id=-123))
        self.assertGreaterEqual(limiter._next_chat[-123] - time.monotonic(), 2.8)


if __name__ == "__main__":
    unittest.main()
