"""Exercise the real operator routers without any Telegram network calls."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import CallbackQuery, Chat, Message, MessageId, Update, User

from bot.handlers import create_root_router

ADMIN = 101
USER = 202
INBOX = -303


class FakePool:
    def __init__(self) -> None:
        self.sessions: set[int] = set()
        self.mappings: dict[tuple[int, int], int] = {}
        self.users: set[int] = set()
        self.broadcasts = 0
        self.broadcast_states: dict[int, str] = {}

    async def execute(self, query, *args):
        if "INSERT INTO support_sessions" in query:
            self.sessions.add(args[0])
        elif "DELETE FROM support_sessions" in query:
            self.sessions.discard(args[0])
        elif "INSERT INTO support_messages" in query:
            self.mappings[(args[0], args[1])] = args[2]
        return "OK"

    async def fetchval(self, query, *args):
        if "SELECT EXISTS(SELECT 1 FROM support_sessions" in query:
            return args[0] in self.sessions or (args[0], args[1]) in self.mappings
        if "FROM support_messages" in query:
            return self.mappings.get((args[0], args[1]))
        if "FROM users WHERE telegram_id" in query:
            return args[0] if args[0] in self.users else None
        if "INSERT INTO broadcasts" in query:
            self.broadcasts += 1
            self.broadcast_states[self.broadcasts] = "draft"
            return self.broadcasts
        if "UPDATE broadcasts SET state='cancelled'" in query:
            if self.broadcast_states.get(args[0]) == "draft":
                self.broadcast_states[args[0]] = "cancelled"
                return args[0]
            return None
        if "SELECT count(*) FROM users WHERE notifications_enabled" in query:
            return len(self.users)
        return None

    async def fetchrow(self, query, *_args):
        if "WITH active" in query:
            return {"active_users": 1, "new_users": 1, "returning_users": 0}
        return {"total": 0, "bytes": 0, "avg_wait": None,
                "p95_wait": None, "avg_seconds": None}

    async def fetch(self, *_args):
        return []


class FakeDatabase:
    def __init__(self) -> None:
        self.pool = FakePool()
        self.events: list[tuple[int, str, str]] = []

    async def upsert_user(self, telegram_id, _username):
        self.pool.users.add(telegram_id)

    async def record_event(self, telegram_id, action, outcome, _duration):
        self.events.append((telegram_id, action, outcome))


class OperatorDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.patcher = patch("bot.handlers.support.os.getenv", return_value=str(INBOX))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.db = FakeDatabase()
        self.session = AiohttpSession()
        self.calls: list[tuple[str, object]] = []
        self.next_id = 900

        async def fake_request(_bot, method, **_kwargs):
            self.calls.append((method.__api_method__, method))
            self.next_id += 1
            if method.__api_method__ == "sendMessage":
                return Message(
                    message_id=self.next_id, date=datetime.now(timezone.utc),
                    chat=self._chat(int(method.chat_id)), text=method.text,
                    from_user=User(id=999, is_bot=True, first_name="Bot"),
                )
            if method.__api_method__ == "copyMessage":
                return MessageId(message_id=self.next_id)
            return True

        self.session.make_request = fake_request
        self.bot = Bot(token="123456:TEST", session=self.session)
        self.dispatcher = Dispatcher(db=self.db)
        self.dispatcher.include_router(create_root_router(ADMIN))
        self.update_id = 0

    async def asyncTearDown(self) -> None:
        await self.session.close()

    @staticmethod
    def _chat(chat_id: int) -> Chat:
        return Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")

    @staticmethod
    def _user(user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name="Operator" if user_id == ADMIN else "User")

    def _message(self, user_id: int, chat_id: int, text: str,
                 reply: Message | None = None) -> Message:
        self.next_id += 1
        return Message(
            message_id=self.next_id, date=datetime.now(timezone.utc),
            chat=self._chat(chat_id), from_user=self._user(user_id),
            text=text, reply_to_message=reply,
        )

    async def _feed_message(self, message: Message) -> None:
        self.update_id += 1
        await self.dispatcher.feed_update(
            self.bot, Update(update_id=self.update_id, message=message),
        )

    async def _check_contact_relay_reply_mapping_and_end(self) -> None:
        await self._feed_message(self._message(USER, USER, "/contact"))
        self.assertIn(USER, self.db.pool.sessions)
        await self._feed_message(self._message(USER, USER, "Здравствуйте"))
        self.assertTrue(any(name == "copyMessage" and method.chat_id == INBOX
                            for name, method in self.calls))
        mapped = next(mid for (chat, mid), uid in self.db.pool.mappings.items()
                      if chat == INBOX and uid == USER)
        header = Message(message_id=mapped, date=datetime.now(timezone.utc),
                         chat=self._chat(INBOX), from_user=User(
                             id=999, is_bot=True, first_name="Bot"))
        await self._feed_message(self._message(ADMIN, INBOX, "Ответ", reply=header))
        self.assertTrue(any(name == "copyMessage" and method.chat_id == USER
                            for name, method in self.calls))
        await self._feed_message(self._message(USER, USER, "/end"))
        self.assertNotIn(USER, self.db.pool.sessions)

    async def _check_unknown_tell_never_sends_to_target(self) -> None:
        await self._feed_message(self._message(ADMIN, ADMIN, "/tell 999999 hello"))
        self.assertFalse(any(name in {"sendMessage", "copyMessage"}
                             and int(method.chat_id) == 999999 for name, method in self.calls))
        self.assertTrue(any("не найден" in (method.text or "") for name, method in self.calls
                            if name == "sendMessage"))

    async def _check_broadcast_draft_and_confirm_are_admin_only(self) -> None:
        previous = len(self.calls)
        await self._feed_message(self._message(USER, USER, "/broadcast hello"))
        self.assertEqual(self.db.pool.broadcasts, 0)
        await self._feed_message(self._message(ADMIN, ADMIN, "/broadcast hello"))
        self.assertEqual(self.db.pool.broadcasts, 1)
        self.assertFalse(any(name == "copyMessage" and method.chat_id == USER
                             for name, method in self.calls[previous:]))

        async def callback(user_id):
            self.update_id += 1
            update = Update(update_id=self.update_id, callback_query=CallbackQuery(
                id=f"click-{self.update_id}", from_user=self._user(user_id),
                chat_instance="test", data="broadcast:start:1",
                message=self._message(999, ADMIN, "Черновик"),
            ))
            await self.dispatcher.feed_update(self.bot, update)

        with patch("bot.handlers.broadcasts.launch", new_callable=AsyncMock) as launch:
            launch.return_value = True
            await callback(USER)
            launch.assert_not_awaited()
            await callback(ADMIN)
            launch.assert_awaited_once_with(self.db, 1)

    async def _check_dashboard_issue_and_revoke_are_private_owner_only(self) -> None:
        with patch("bot.handlers.analytics._open_dashboard", new_callable=AsyncMock) as issue:
            await self._feed_message(self._message(USER, USER, "/dashboard"))
            await self._feed_message(self._message(ADMIN, INBOX, "/dashboard"))
            issue.assert_not_awaited()
            await self._feed_message(self._message(ADMIN, ADMIN, "/dashboard"))
            issue.assert_awaited_once()
        with patch("bot.dashboard.DashboardAccess.revoke", new_callable=AsyncMock) as revoke:
            for user_id, chat_id in ((USER, USER), (ADMIN, INBOX)):
                self.update_id += 1
                await self.dispatcher.feed_update(self.bot, Update(
                    update_id=self.update_id, callback_query=CallbackQuery(
                        id=f"close-{self.update_id}", from_user=self._user(user_id),
                        chat_instance="test", data="dashboardclose",
                        message=self._message(999, chat_id, "Access"),
                    ),
                ))
            revoke.assert_not_awaited()

    async def _check_analytics_is_private_admin_command(self) -> None:
        await self._feed_message(self._message(ADMIN, ADMIN, "/analytics"))
        self.assertTrue(any("Аналитика за 24h" in (method.text or "")
                            for name, method in self.calls if name == "sendMessage"))
        previous = len(self.calls)
        await self._feed_message(self._message(ADMIN, INBOX, "/analytics"))
        self.assertFalse(any("Аналитика за 24h" in (method.text or "")
                             for name, method in self.calls[previous:] if name == "sendMessage"))

    async def _check_group_draft_can_be_cancelled_without_launch(self) -> None:
        bid = self.db.pool.broadcasts + 1
        previous = len(self.calls)
        await self._feed_message(self._message(ADMIN, INBOX, "/broadcast test draft"))
        self.assertEqual(self.db.pool.broadcast_states[bid], "draft")
        self.update_id += 1
        callback = CallbackQuery(
            id="cancel-draft", from_user=self._user(ADMIN), chat_instance="test",
            data=f"broadcast:cancel:{bid}",
            message=self._message(999, INBOX, "Черновик"),
        )
        with patch("bot.handlers.broadcasts.launch", new_callable=AsyncMock) as launch:
            await self.dispatcher.feed_update(
                self.bot, Update(update_id=self.update_id, callback_query=callback),
            )
            launch.assert_not_awaited()
        self.assertEqual(self.db.pool.broadcast_states[bid], "cancelled")
        self.assertFalse(any(
            name in {"sendMessage", "copyMessage"} and int(method.chat_id) == USER
            for name, method in self.calls[previous:]
        ))

    async def test_operator_flows(self) -> None:
        await self._check_contact_relay_reply_mapping_and_end()
        await self._check_unknown_tell_never_sends_to_target()
        await self._check_broadcast_draft_and_confirm_are_admin_only()
        await self._check_analytics_is_private_admin_command()
        await self._check_dashboard_issue_and_revoke_are_private_owner_only()
        await self._check_group_draft_can_be_cancelled_without_launch()
        await self._check_legacy_subscription_commands_cannot_change_delivery()

    async def _check_legacy_subscription_commands_cannot_change_delivery(self) -> None:
        for command in ("/unsubscribe", "/subscribe"):
            previous = len(self.calls)
            with patch.object(self.db.pool, "execute", new_callable=AsyncMock) as execute:
                await self._feed_message(self._message(USER, USER, command))
            self.assertFalse(any(
                "notifications_enabled" in call.args[0] for call in execute.await_args_list
            ))
            self.assertFalse(any(
                "Объявления отключены" in (method.text or "")
                for name, method in self.calls[previous:] if name == "sendMessage"
            ))


if __name__ == "__main__":
    unittest.main()
