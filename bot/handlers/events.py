"""Small, bounded action journal; never stores message bodies or callback values."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)
_handled_outcome: ContextVar[str | None] = ContextVar("handled_event_outcome", default=None)


def mark_outcome(outcome: str) -> None:
    """Record a handled download result in the current update's event journal."""
    if outcome in {"done", "failed", "cancelled", "rate_limited"}:
        _handled_outcome.set(outcome)


def action_name(event: TelegramObject) -> str:
    if isinstance(event, CallbackQuery):
        # Prefix only: callback data can contain URLs, tokens or user input.
        prefix = (event.data or "").split(":", 1)[0]
        return "callback:" + (prefix if prefix.isascii() and prefix.isalnum() else "other")
    if isinstance(event, Message):
        if event.text and event.text.startswith("/"):
            command = event.text.split(maxsplit=1)[0].split("@", 1)[0][1:]
            if command.isascii() and command.replace("_", "").isalnum():
                return "command:" + command[:32]
        if event.text:
            return "message:text"
        return "message:media"
    return "other"


class EventMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        db = data.get("db")
        if user is None or db is None:
            return await handler(event, data)
        started = time.monotonic()
        outcome = "ok"
        token = _handled_outcome.set(None)
        try:
            return await handler(event, data)
        except Exception:
            outcome = "error"
            raise
        finally:
            if outcome == "ok" and _handled_outcome.get() is not None:
                outcome = _handled_outcome.get()
            _handled_outcome.reset(token)
            try:
                await db.record_event(
                    user.id, action_name(event), outcome,
                    min(int((time.monotonic() - started) * 1000), 2_147_483_647),
                )
            except Exception:
                logger.warning("Could not record user event", exc_info=True)
