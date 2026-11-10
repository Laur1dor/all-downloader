"""Small, bounded action journal; never stores message bodies or callback values."""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)
_handled_outcome: ContextVar[str | None] = ContextVar("handled_event_outcome", default=None)


def mark_outcome(outcome: str) -> str:
    """Record a handled download result in the current update's event journal."""
    if outcome in {"done", "failed", "cancelled", "rate_limited", "delivery_unknown"}:
        from bot import observability
        import sys
        job = observability._job.get()
        error = sys.exc_info()[1]
        if job and error is not None:
            job.reason = type(error).__name__
            if error.__cause__:
                observability.request_context(cause_class=type(error.__cause__).__name__)
        if outcome=='failed' and observability.refused_input(error):
            outcome='rejected'
        _handled_outcome.set(outcome)
        observability.outcome(outcome)
    return outcome


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
            from bot.observability import run_event
            return await run_event(handler, event, data)
        started = time.monotonic()
        outcome = "ok"
        token = _handled_outcome.set(None)
        try:
            from bot.observability import run_event
            return await run_event(handler, event, data)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            reported = data.get('_operational_outcome') or _handled_outcome.get()
            if outcome == "ok" and reported in {"done","failed","cancelled","rate_limited","delivery_unknown","rejected"}:
                outcome = reported
            _handled_outcome.reset(token)
            try:
                if not data.get('_audit_refused') and outcome != 'rate_limited':
                    await asyncio.wait_for(db.pool.execute(
                        "UPDATE users SET language_code=$2 WHERE telegram_id=$1",
                        user.id, user.language_code,
                    ), timeout=3)
                    await asyncio.wait_for(db.record_event(
                        user.id, action_name(event), outcome,
                        min(int((time.monotonic() - started) * 1000), 2_147_483_647),
                    ),timeout=3)
            except Exception:
                logger.warning("Could not record user event", exc_info=True)
