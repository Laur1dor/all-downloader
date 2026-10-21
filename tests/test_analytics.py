"""Analytics reports expose useful counts without message or callback contents."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.types import TelegramObject

from bot.analytics import export_csv, summary
from bot.handlers.events import EventMiddleware, mark_outcome


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "WITH active" in query:
            return {"active_users": 12, "new_users": 4, "returning_users": 8}
        return {"total": 2, "bytes": 1048576, "avg_wait": 1.5,
                "p95_wait": 3.0, "avg_seconds": 10.0}

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "GROUP BY 1, 2" in query:
            return [
                {"platform": "youtube", "status": "done", "total": 3,
                 "avg_seconds": 12.5, "bytes": 1048576},
                {"platform": "youtube", "status": "failed", "total": 1,
                 "avg_seconds": 2.0, "bytes": 0},
                {"platform": "youtube", "status": "delivery_unknown", "total": 2,
                 "avg_seconds": 120.0, "bytes": 0},
            ]
        if "GROUP BY action" in query:
            return [{"action": "callback:quality", "outcome": "ok",
                     "total": 2, "avg_ms": 100}]
        stamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
        if "FROM user_events" in query:
            return [{"created_at": stamp, "telegram_id": 123,
                     "action": "callback:quality", "outcome": "failed",
                     "duration_ms": 100}]
        if "FROM conversions" in query:
            return [{"created_at": stamp, "telegram_id": 123,
                     "source_platform": "youtube", "media_type": "video",
                     "status": "failed", "file_size": 0, "processing_time": 2.0}]
        return [{"created_at": stamp, "platform": "youtube", "lane": "small",
                 "is_admin": False, "size_bytes": 1, "seconds": 0.5,
                 "waited_seconds": 0.1, "alongside": 0}]


class AnalyticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_uses_bounded_period_and_correct_totals(self) -> None:
        pool = FakePool()
        output = await summary(SimpleNamespace(pool=pool), "7d")
        self.assertIn("Активных: <b>12</b>", output)
        self.assertIn("<b>3</b> успешных · 1 ошибок", output)
        self.assertIn("callback:quality", output)
        self.assertIn("2 доставок не подтверждено", output)
        self.assertTrue(all(args == (168,) for _, args in pool.calls))

    async def test_export_has_only_structured_fields_and_row_limits(self) -> None:
        pool = FakePool()
        output = (await export_csv(SimpleNamespace(pool=pool), "30d")).decode("utf-8-sig")
        self.assertIn("events\r\ncreated_at,telegram_id,action,outcome,duration_ms", output)
        self.assertIn("conversions\r\n", output)
        self.assertIn("uploads\r\n", output)
        self.assertNotIn("message_body", output)
        self.assertNotIn("callback_data", output)
        self.assertTrue(all(args == (720, 5000) for _, args in pool.calls))

    async def test_handled_failure_is_not_recorded_as_ok(self) -> None:
        recorded: list[str] = []

        class Db:
            async def record_event(self, _user, _action, outcome, _duration):
                recorded.append(outcome)

        async def failed(_event, _data):
            mark_outcome("failed")

        async def normal(_event, _data):
            pass

        middleware = EventMiddleware()
        data = {"event_from_user": SimpleNamespace(id=123), "db": Db()}
        await middleware(failed, TelegramObject(), data)
        await middleware(normal, TelegramObject(), data)
        self.assertEqual(recorded, ["failed", "ok"])


    async def test_uncertain_delivery_and_cancellation_are_not_ok_or_failed(self) -> None:
        recorded = []

        class Db:
            async def record_event(self, _user, _action, outcome, _duration):
                recorded.append(outcome)

        async def unknown(_event, _data):
            mark_outcome("delivery_unknown")

        async def cancelled(_event, _data):
            raise asyncio.CancelledError

        async def normal(_event, _data):
            pass

        middleware = EventMiddleware()
        data = {"event_from_user": SimpleNamespace(id=123), "db": Db()}
        await middleware(unknown, TelegramObject(), data)
        with self.assertRaises(asyncio.CancelledError):
            await middleware(cancelled, TelegramObject(), data)
        await middleware(normal, TelegramObject(), data)
        self.assertEqual(recorded, ["delivery_unknown", "cancelled", "ok"])


if __name__ == "__main__":
    unittest.main()
