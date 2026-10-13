"""Automatic Instagram login limits survive bot restarts and crashes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot import igsession


class AutoLoginBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_checkpoint_redirect_is_not_a_network_error(self):
        self.assertEqual(igsession._read_answer(
            200, '<html>', 'https://www.instagram.com/accounts/suspended/',
        ), (igsession.CHECKPOINT, None))
        for url, status in (
            ('https://www.instagram.com/', 200),
            ('https://other.example/challenge/', 200),
            ('https://www.instagram.com/challenge/', 429),
        ):
            self.assertEqual(igsession._read_answer(status, '<html>', url), (igsession.UNKNOWN, None))

    async def test_manual_recovery_marker_stops_automatic_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'ig_account_blocked.json').write_text('{"reason":"phone_required"}')
            watch = igsession.InstagramSession(folder / 'cookies', folder / 'request', folder / 'result')
            watch._ask_for_login = AsyncMock(return_value=(False, 'manual confirmation'))
            await watch._sign_in_again()
            watch._ask_for_login.assert_not_awaited()
            await watch._went_live('owner')
            self.assertFalse((folder / 'ig_account_blocked.json').exists())
    async def test_cooldown_and_daily_cap_survive_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            clock = SimpleNamespace(now=1000.0)
            with patch.object(
                igsession, "time", SimpleNamespace(time=lambda: clock.now)
            ):
                for attempt in range(igsession._LOGINS_PER_DAY):
                    watch = igsession.InstagramSession(
                        folder / "cookies", folder / "request", folder / "result"
                    )
                    ask = AsyncMock(return_value=(False, "checkpoint"))
                    watch._ask_for_login = ask
                    await watch._sign_in_again()
                    ask.assert_awaited_once()
                    self.assertEqual(len(watch._logins), attempt + 1)
                    self.assertGreater(watch._next_attempt, clock.now)

                    restarted = igsession.InstagramSession(
                        folder / "cookies", folder / "request", folder / "result"
                    )
                    no_retry = AsyncMock(return_value=(False, "checkpoint"))
                    restarted._ask_for_login = no_retry
                    await restarted._sign_in_again()
                    no_retry.assert_not_awaited()
                    clock.now = watch._next_attempt + 1

                capped = igsession.InstagramSession(
                    folder / "cookies", folder / "request", folder / "result"
                )
                auto = AsyncMock(return_value=(False, "checkpoint"))
                capped._ask_for_login = auto
                await capped._sign_in_again()
                auto.assert_not_awaited()

                # Explicit operator action remains available after the auto cap.
                manual = await capped.login_now()
                self.assertEqual(manual, (False, "checkpoint"))
                auto.assert_awaited_once()

    async def test_crash_after_reservation_cannot_retry_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(
                igsession, "time", SimpleNamespace(time=lambda: 1000.0)
            ):
                watch = igsession.InstagramSession(
                    folder / "cookies", folder / "request", folder / "result"
                )
                watch._ask_for_login = AsyncMock(side_effect=RuntimeError("crash"))
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    await watch._sign_in_again()

                restarted = igsession.InstagramSession(
                    folder / "cookies", folder / "request", folder / "result"
                )
                restarted._ask_for_login = AsyncMock()
                await restarted._sign_in_again()
                restarted._ask_for_login.assert_not_awaited()
                self.assertEqual(len(restarted._logins), 1)

    async def test_unreadable_budget_pauses_only_automatic_login(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "igsession_auto_login.json").write_text("{broken")
            watch = igsession.InstagramSession(
                folder / "cookies", folder / "request", folder / "result"
            )
            ask = AsyncMock(return_value=(False, "manual answer"))
            watch._ask_for_login = ask
            await watch._sign_in_again()
            ask.assert_not_awaited()
            self.assertEqual(await watch.login_now(), (False, "manual answer"))
            ask.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
