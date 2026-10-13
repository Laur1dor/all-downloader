"""Large media calls must pass their deadline through aiogram's session."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, FSInputFile, Message

from bot.handlers.download import _send_paced


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
