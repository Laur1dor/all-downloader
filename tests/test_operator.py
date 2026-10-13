"""Announcement safety and registered-user recipient selection."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import CopyMessage

from bot.broadcasts import launch, run_worker
from bot.operator import prepare, recipient


class Scope:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_does_not_repeat_uncertain_delivery(self):
        db = SimpleNamespace(pool=SimpleNamespace(execute=AsyncMock()))
        await prepare(db)
        sql = db.pool.execute.call_args.args[0]
        self.assertIn("state='uncertain' WHERE state='sending'", sql)

    async def test_numeric_recipient_must_exist_in_users(self):
        pool = SimpleNamespace(fetchval=AsyncMock(return_value=None))
        self.assertIsNone(await recipient(SimpleNamespace(pool=pool), "999"))
        self.assertEqual(pool.fetchval.call_args.args[1], 999)
        self.assertIn("FROM users", pool.fetchval.call_args.args[0])

    async def test_launch_snapshots_deliverable_users_once(self):
        conn = SimpleNamespace(fetchval=AsyncMock(return_value=4), execute=AsyncMock())
        conn.transaction = lambda: Scope(None)
        db = SimpleNamespace(pool=SimpleNamespace(acquire=lambda: Scope(conn)))
        self.assertTrue(await launch(db, 4))
        self.assertIn("notifications_enabled", conn.execute.call_args.args[0])
        conn.fetchval.return_value = None
        conn.execute.reset_mock()
        self.assertFalse(await launch(db, 4))
        conn.execute.assert_not_awaited()

    async def delivery(self, allowed=True, failure=None, attempts=1):
        row = {"broadcast_id": 1, "telegram_id": 42, "source_chat": 7,
               "source_message": 8, "attempts": attempts}
        pool = SimpleNamespace(fetchrow=AsyncMock(return_value=row),
                               fetchval=AsyncMock(return_value=allowed), execute=AsyncMock())
        bot = SimpleNamespace(copy_message=AsyncMock(side_effect=failure))
        async def stop(_seconds):
            raise asyncio.CancelledError
        with patch("bot.broadcasts.asyncio.sleep", stop), self.assertRaises(asyncio.CancelledError):
            await run_worker(bot, SimpleNamespace(pool=pool))
        return bot, pool.execute.call_args.args

    async def test_cancel_or_blocked_delivery_before_send_skips_recipient(self):
        bot, update = await self.delivery(allowed=False)
        bot.copy_message.assert_not_awaited()
        self.assertEqual(update[3], "skipped")

    async def test_network_timeout_is_not_retried(self):
        method = CopyMessage(chat_id=42, from_chat_id=7, message_id=8)
        bot, update = await self.delivery(failure=TelegramNetworkError(method, "timeout"))
        self.assertEqual(bot.copy_message.await_count, 1)
        self.assertEqual(update[3], "uncertain")

    async def test_explicit_rate_limit_has_bounded_retries(self):
        method = CopyMessage(chat_id=42, from_chat_id=7, message_id=8)
        failure = TelegramRetryAfter(method, "flood", retry_after=5)
        _, update = await self.delivery(failure=failure, attempts=3)
        self.assertEqual(update[3], "failed")
        _, update = await self.delivery(failure=failure, attempts=1)
        self.assertEqual(update[3], "pending")
        self.assertEqual(update[5], 5.0)


if __name__ == "__main__":
    unittest.main()
