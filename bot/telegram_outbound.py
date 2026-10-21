"""Bounded, priority-aware admission for outgoing Telegram Bot API calls."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger(__name__)

_progress_call: ContextVar[bool] = ContextVar("telegram_progress_call", default=False)
_bulk_call: ContextVar[bool] = ContextVar("telegram_bulk_call", default=False)
_MEDIA_METHODS = frozenset({
    "sendAnimation", "sendAudio", "sendDocument", "sendMediaGroup", "sendLivePhoto",
    "sendPhoto", "sendVideo", "sendVideoNote", "sendVoice",
})
_READ_METHODS = frozenset({"getUpdates", "getFile", "getMe"})
_MAX_QUEUE = 80
_MAX_BULK_QUEUE = 60
_MAX_WAIT = 120.0


@contextmanager
def progress_call():
    """Mark disposable progress edits so replies and completed media go first."""
    token = _progress_call.set(True)
    try:
        yield
    finally:
        _progress_call.reset(token)


@contextmanager
def bulk_call():
    """Place an operator broadcast behind user replies and downloads."""
    token = _bulk_call.set(True)
    try:
        yield
    finally:
        _bulk_call.reset(token)


@dataclass(eq=False)
class _Waiter:
    priority: int
    sequence: int
    media: bool
    cost: int
    chat: int | str | None


class OutgoingLimiter(BaseRequestMiddleware):
    """Share flood waits, with a finite queue and one safe retry after a 429.

    Telegram rejects a request with RetryAfter before accepting its payload, so
    retrying that explicit response cannot duplicate an upload. Network errors
    are deliberately left to the caller, which knows whether a retry is safe.
    """

    def __init__(self, *, media_spacing: float | None = None) -> None:
        self._condition = asyncio.Condition()
        self._queue: list[_Waiter] = []
        self._sequence = 0
        self._cooldown_until = 0.0
        self._next_request = 0.0
        self._next_media = 0.0
        self._next_chat: dict[int | str, float] = {}
        self._high_streak = 0
        self._media_spacing = max(0.5, media_spacing if media_spacing is not None
                                  else float(os.getenv("MEDIA_SEND_SPACING_SECONDS", "2.5")))

    @staticmethod
    def _priority(method: Any) -> tuple[int, bool, int]:
        name = method.__api_method__
        if _progress_call.get():
            return 4, False, 1
        if _bulk_call.get():
            return 3, False, 1
        if name in _MEDIA_METHODS:
            cost = len(method.media) if name == "sendMediaGroup" else 1
            return 2, True, max(1, min(cost, 10))
        if name.startswith(("edit", "delete")):
            return 1, False, 1
        return 0, False, 1

    @staticmethod
    def _chat(method: Any) -> int | str | None:
        chat = getattr(method, "chat_id", None)
        if isinstance(chat, int):
            return chat
        if isinstance(chat, str) and chat:
            try:
                return int(chat)
            except ValueError:
                return chat
        return None

    @staticmethod
    def _chat_spacing(chat: int | str) -> float:
        return 3.0 if isinstance(chat, str) or chat < 0 else 1.0

    def _ready_at(self, waiter: _Waiter) -> float:
        return max(
            self._cooldown_until, self._next_request,
            self._next_media if waiter.media else 0,
            self._next_chat.get(waiter.chat, 0) if waiter.chat is not None else 0,
        )

    async def _admit(self, method: Any, deadline: float) -> None:
        priority, media, cost = self._priority(method)
        chat = self._chat(method)
        async with self._condition:
            if len(self._queue) >= _MAX_QUEUE or (
                priority >= 2 and sum(w.priority >= 2 for w in self._queue) >= _MAX_BULK_QUEUE
            ):
                raise TelegramRetryAfter(method, "Telegram send queue full", retry_after=5)
            self._sequence += 1
            waiter = _Waiter(priority, self._sequence, media, cost, chat)
            self._queue.append(waiter)
            try:
                while True:
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TelegramRetryAfter(
                            method, "Telegram send queue timed out", retry_after=5
                        )
                    eligible = [w for w in self._queue if self._ready_at(w) <= now]
                    lower = [w for w in eligible if w.priority >= 2]
                    chosen = min(lower if self._high_streak >= 10 and lower else eligible,
                                 key=lambda w: (w.priority, w.sequence), default=None)
                    if waiter is chosen:
                        self._queue.remove(waiter)
                        self._next_request = now + 0.05
                        if media:
                            self._next_media = now + self._media_spacing * cost
                        if chat is not None:
                            self._next_chat[chat] = now + self._chat_spacing(chat) * cost
                            if len(self._next_chat) > 2048:
                                self._next_chat = {
                                    key: expiry for key, expiry in self._next_chat.items()
                                    if expiry > now
                                }
                        self._high_streak = self._high_streak + 1 if priority <= 1 else 0
                        self._condition.notify_all()
                        return
                    # Wake when a timer expires even when no other task notifies.
                    next_ready = min(self._ready_at(w) for w in self._queue)
                    delay = min(remaining, max(0.01, next_ready - now))
                    try:
                        await asyncio.wait_for(self._condition.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if waiter in self._queue:
                    self._queue.remove(waiter)
                    self._condition.notify_all()

    async def __call__(self, make_request: Callable[..., Any], bot: Any,
                       method: Any) -> Any:
        if method.__api_method__ in _READ_METHODS:
            return await make_request(bot, method)
        deadline = time.monotonic() + _MAX_WAIT
        for attempt in range(2):
            await self._admit(method, deadline)
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                seconds = max(0.1, float(exc.retry_after))
                async with self._condition:
                    self._cooldown_until = max(self._cooldown_until,
                                               time.monotonic() + seconds)
                    self._condition.notify_all()
                logger.warning("Telegram flood wait: %.1fs on %s", seconds,
                               method.__api_method__)
                if attempt or seconds > _MAX_WAIT or time.monotonic() + seconds >= deadline:
                    raise
        raise AssertionError("unreachable")
