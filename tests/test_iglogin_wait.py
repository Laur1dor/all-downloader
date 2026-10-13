"""The browser may finish its checkpoint just after the former 420s limit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import igsession
from bot.handlers import admin


class LoginWaitTests(unittest.IsolatedAsyncioTestCase):
    async def _run_case(self, *, watch: bool, result_at: int | None):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request"
            result = Path(directory) / "result"
            clock = SimpleNamespace(now=0)

            async def advance(seconds):
                clock.now += seconds
                if result_at is not None and clock.now >= result_at:
                    result.write_text("FAIL\ncheckpoint needs a person", encoding="utf-8")

            fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=lambda: 1)
            fake_asyncio = SimpleNamespace(sleep=advance)
            if watch:
                session = igsession.InstagramSession(None, request, result)
                with patch.object(igsession, "time", fake_time), patch.object(
                    igsession, "asyncio", fake_asyncio
                ):
                    answer = await session._ask_for_login()
            else:
                with (
                    patch.object(admin, "_IGLOGIN_REQUEST", request),
                    patch.object(admin, "_IGLOGIN_RESULT", result),
                    patch.object(admin, "time", fake_time),
                    patch.object(admin, "asyncio", fake_asyncio),
                ):
                    answer = await admin._run_iglogin()
            self.assertTrue(request.exists())
            return answer, clock.now

    async def test_late_browser_result_is_returned_by_both_paths(self):
        for watch in (True, False):
            with self.subTest(watch=watch):
                answer, elapsed = await self._run_case(watch=watch, result_at=425)
                self.assertEqual(answer, (False, "checkpoint needs a person"))
                self.assertEqual(elapsed, 425)

    async def test_real_timeout_does_not_claim_container_is_down(self):
        for watch in (True, False):
            with self.subTest(watch=watch):
                answer, elapsed = await self._run_case(watch=watch, result_at=None)
                self.assertFalse(answer[0])
                self.assertEqual(elapsed, 600)
                self.assertIn("10 минут", answer[1])
                self.assertNotIn("запущен?", answer[1])


if __name__ == "__main__":
    unittest.main()
