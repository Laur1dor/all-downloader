"""Bounded, resumable announcements with explicit launch confirmation."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from bot.db import Database
from bot.telegram_outbound import bulk_call

logger = logging.getLogger(__name__)


async def launch(db: Database, broadcast_id: int) -> bool:
    async with db.pool.acquire() as conn, conn.transaction():
        changed = await conn.fetchval(
            "UPDATE broadcasts SET state='running' WHERE id=$1 AND state='draft' RETURNING id",
            broadcast_id,
        )
        if changed is None:
            return False
        await conn.execute(
            "INSERT INTO broadcast_recipients(broadcast_id,telegram_id) "
            "SELECT $1,telegram_id FROM users WHERE notifications_enabled ON CONFLICT DO NOTHING",
            broadcast_id,
        )
    return True


async def run_worker(bot: Bot, db: Database) -> None:
    while True:
        row = await db.pool.fetchrow(
            "WITH picked AS (SELECT r.broadcast_id,r.telegram_id FROM broadcast_recipients r "
            "JOIN broadcasts b ON b.id=r.broadcast_id JOIN users u ON u.telegram_id=r.telegram_id "
            "WHERE b.state='running' AND r.state='pending' AND r.next_attempt_at<=now() "
            "AND u.notifications_enabled ORDER BY b.id,r.telegram_id "
            "FOR UPDATE OF r SKIP LOCKED LIMIT 1) "
            "UPDATE broadcast_recipients r SET state='sending',attempts=attempts+1 "
            "FROM picked p,broadcasts b WHERE r.broadcast_id=p.broadcast_id "
            "AND r.telegram_id=p.telegram_id AND b.id=p.broadcast_id "
            "RETURNING r.*,b.source_chat,b.source_message"
        )
        if row is None:
            await db.pool.execute(
                "UPDATE broadcasts b SET state='done' WHERE state='running' AND NOT EXISTS "
                "(SELECT 1 FROM broadcast_recipients r JOIN users u ON u.telegram_id=r.telegram_id "
                "WHERE r.broadcast_id=b.id AND r.state IN ('pending','sending') "
                "AND u.notifications_enabled)"
            )
            await asyncio.sleep(2)
            continue
        state, error, delay = "sent", None, 0
        try:
            with bulk_call():
                # Recheck after claim: cancellation and blocked delivery take effect before
                # starting the next request; an in-flight request cannot be recalled.
                allowed = await db.pool.fetchval(
                    "SELECT b.state='running' AND u.notifications_enabled "
                    "FROM broadcasts b,users u "
                    "WHERE b.id=$1 AND u.telegram_id=$2",
                    row["broadcast_id"],
                    row["telegram_id"],
                )
                if not allowed:
                    state = "skipped"
                else:
                    await bot.copy_message(
                        row["telegram_id"], row["source_chat"], row["source_message"]
                    )
        except TelegramForbiddenError:
            state = "blocked"
            await db.pool.execute(
                "UPDATE users SET notifications_enabled=FALSE WHERE telegram_id=$1",
                row["telegram_id"],
            )
        except TelegramRetryAfter as exc:
            state = "pending" if row["attempts"] < 3 else "failed"
            delay = min(max(exc.retry_after, 1), 86400)
            error = "rate_limit"
        except TelegramBadRequest:
            state, error = "failed", "invalid_message_or_recipient"
        except (TelegramAPIError, TimeoutError, OSError):
            state, error = "uncertain", "delivery_unknown"
        await db.pool.execute(
            "UPDATE broadcast_recipients SET state=$3,error=$4,"
            "next_attempt_at=now()+make_interval(secs=>$5) "
            "WHERE broadcast_id=$1 AND telegram_id=$2",
            row["broadcast_id"],
            row["telegram_id"],
            state,
            error,
            float(delay),
        )
        await asyncio.sleep(1)


async def supervised_worker(bot: Bot, db: Database) -> None:
    while True:
        try:
            await run_worker(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Announcement worker failed; recovering in 10 seconds")
            await db.pool.execute(
                "UPDATE broadcast_recipients SET state='uncertain' WHERE state='sending'"
            )
            await asyncio.sleep(10)
