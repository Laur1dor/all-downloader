"""A ceiling on messages per user, above the per-download budget.

The download budget limits work; this limits *talking*. They are different
things and both are needed: pasting the same link twenty times costs almost no
work, because the second one is answered from cache — but it is still twenty
updates, twenty handler tasks, twenty replies, and twenty rows of somebody
else's attention. A limit that only counted downloads would wave all of that
through.

It sits in front of every router rather than inside a handler, so a message that
is over the line never reaches one, and the cost of refusing it stays near zero.
"""

from __future__ import annotations

import logging
import os
import time

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

logger = logging.getLogger(__name__)

# Seven a minute: comfortably above anyone typing normally, and far below what
# it takes to make the machine care.
FLOOD_MESSAGES = float(os.getenv("FLOOD_MESSAGES", "7"))
FLOOD_WINDOW_SECONDS = float(os.getenv("FLOOD_WINDOW_SECONDS", "60"))
# Somebody over the limit is told once and then ignored until they stop, because
# answering every message of a flood is itself a flood.
_NOTICE_EVERY_SECONDS = 60.0


class FloodMiddleware(BaseMiddleware):
    def __init__(self, admin_id: int) -> None:
        self._admin_id = admin_id
        self._buckets: dict[int, tuple[float, float]] = {}
        self._told: dict[int, float] = {}

    def _allow(self, user_id: int) -> bool:
        now = time.monotonic()
        tokens, stamp = self._buckets.get(user_id, (FLOOD_MESSAGES, now))
        tokens = min(
            FLOOD_MESSAGES,
            tokens + (now - stamp) * FLOOD_MESSAGES / FLOOD_WINDOW_SECONDS,
        )
        if tokens < 1.0:
            self._buckets[user_id] = (tokens, now)
            return False
        self._buckets[user_id] = (tokens - 1.0, now)
        return True

    def _should_tell(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self._told.get(user_id, 0.0) < _NOTICE_EVERY_SECONDS:
            return False
        self._told[user_id] = now
        return True

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None or user.id == self._admin_id:
            return await handler(event, data)
        if self._allow(user.id):
            return await handler(event, data)
        logger.info("Flood limit hit by %s", user.id)
        if isinstance(event, Message) and self._should_tell(user.id):
            await event.answer(
                "🐢 Слишком много сообщений подряд. Подождите минуту."
            )
        return None
