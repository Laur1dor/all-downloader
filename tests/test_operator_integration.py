"""Opt-in PostgreSQL check for operator migration and delivery transitions.

Run with OPERATOR_DB_INTEGRATION=1 in a container that can reach PostgreSQL.
All objects live in a new schema inside a transaction that is rolled back.
No Telegram API calls are made.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg

from bot.broadcasts import launch, run_worker
from bot.config import load_settings
from bot.operator import prepare, recipient


class _ConnectionPool:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection

    async def execute(self, *args):
        return await self.connection.execute(*args)

    async def fetchval(self, *args):
        return await self.connection.fetchval(*args)

    async def fetchrow(self, *args):
        return await self.connection.fetchrow(*args)


@unittest.skipUnless(os.getenv("OPERATOR_DB_INTEGRATION") == "1", "opt-in database test")
class OperatorDatabaseIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_migration_snapshot_unknown_and_completion(self) -> None:
        connection = await asyncpg.connect(load_settings().database_dsn, timeout=10)
        transaction = connection.transaction()
        await transaction.start()
        try:
            schema = f"operator_audit_{os.getpid()}"
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET LOCAL search_path TO "{schema}"')
            await connection.execute(
                "CREATE TABLE users (telegram_id BIGINT PRIMARY KEY, username TEXT)"
            )
            await connection.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
            db = SimpleNamespace(pool=_ConnectionPool(connection))
            await prepare(db)
            await connection.execute(
                "INSERT INTO users(telegram_id,username,notifications_enabled) "
                "VALUES (101,'audit_one',TRUE),(102,'audit_two',FALSE),(103,'blocked',FALSE)"
            )
            previous_id = await connection.fetchval(
                "INSERT INTO broadcasts(source_chat,source_message,state) "
                "VALUES (7,8,'done') RETURNING id"
            )
            await connection.execute(
                "INSERT INTO broadcast_recipients(broadcast_id,telegram_id,state) "
                "VALUES ($1,103,'blocked')", previous_id,
            )
            await connection.execute("DELETE FROM settings WHERE key='broadcast_all_registered_v1'")
            await prepare(db)
            self.assertTrue(await connection.fetchval(
                "SELECT notifications_enabled FROM users WHERE telegram_id=102"
            ))
            self.assertFalse(await connection.fetchval(
                "SELECT notifications_enabled FROM users WHERE telegram_id=103"
            ))
            # A later Telegram-forbidden delivery must remain disabled on restart.
            await connection.execute(
                "UPDATE users SET notifications_enabled=FALSE WHERE telegram_id=101"
            )
            await prepare(db)
            self.assertFalse(await connection.fetchval(
                "SELECT notifications_enabled FROM users WHERE telegram_id=101"
            ))
            await connection.execute(
                "UPDATE users SET notifications_enabled=TRUE WHERE telegram_id=101"
            )
            self.assertEqual(await recipient(db, "101"), 101)
            self.assertEqual(await recipient(db, "@AUDIT_ONE"), 101)
            self.assertIsNone(await recipient(db, "1020"))

            broadcast_id = await connection.fetchval(
                "INSERT INTO broadcasts(source_chat,source_message) "
                "VALUES (7,8) RETURNING id"
            )
            self.assertTrue(await launch(db, broadcast_id))
            self.assertFalse(await launch(db, broadcast_id))
            recipients = await connection.fetch(
                "SELECT telegram_id FROM broadcast_recipients WHERE broadcast_id=$1 "
                "ORDER BY telegram_id",
                broadcast_id,
            )
            self.assertEqual([row["telegram_id"] for row in recipients], [101, 102])

            async def stop(_seconds):
                raise asyncio.CancelledError

            bot = SimpleNamespace(copy_message=AsyncMock())
            with (
                patch("bot.broadcasts.asyncio.sleep", stop),
                self.assertRaises(asyncio.CancelledError),
            ):
                await run_worker(bot, db)
            bot.copy_message.assert_awaited_once_with(101, 7, 8)
            self.assertEqual(
                await connection.fetchval(
                    "SELECT state FROM broadcast_recipients "
                    "WHERE broadcast_id=$1 AND telegram_id=101", broadcast_id,
                ),
                "sent",
            )

            await connection.execute(
                "UPDATE broadcast_recipients SET state='sending' "
                "WHERE broadcast_id=$1", broadcast_id,
            )
            await prepare(db)
            self.assertEqual(
                await connection.fetchval(
                    "SELECT state FROM broadcast_recipients "
                    "WHERE broadcast_id=$1 AND telegram_id=101", broadcast_id,
                ),
                "uncertain",
            )
            bot.copy_message.reset_mock()
            with (
                patch("bot.broadcasts.asyncio.sleep", stop),
                self.assertRaises(asyncio.CancelledError),
            ):
                await run_worker(bot, db)
            bot.copy_message.assert_not_awaited()
            self.assertEqual(
                await connection.fetchval("SELECT state FROM broadcasts WHERE id=$1", broadcast_id),
                "done",
            )
        finally:
            await transaction.rollback()
            await connection.close()


if __name__ == "__main__":
    unittest.main()
