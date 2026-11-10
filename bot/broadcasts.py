"""Bounded, resumable announcements with explicit launch confirmation."""

from __future__ import annotations

import asyncio
import logging
import json

from aiogram.types import InlineKeyboardMarkup

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


def localized_source(row):
    """Russian Telegram locales use RU; unknown and other locales use EN."""
    variants = row.get("variants")
    if isinstance(variants, str):
        variants = json.loads(variants)
    if not variants:
        return row["source_chat"], row["source_message"], None
    language = (row.get("language_code") or "").lower().replace("_", "-").split("-")[0]
    variant = variants["ru" if language == "ru" else "en"]
    markup = variant.get("reply_markup")
    return (row["source_chat"], int(variant["message_id"]),
            InlineKeyboardMarkup.model_validate(markup) if markup else None)


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
            "RETURNING r.*,b.source_chat,b.source_message,b.variants,"
            "(SELECT language_code FROM users WHERE telegram_id=r.telegram_id) AS language_code"
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
        delivered_message_id = None
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
                    source_chat, source_message, markup = localized_source(row)
                    delivered = await bot.copy_message(
                        row["telegram_id"], source_chat, source_message,
                        **({"reply_markup": markup} if markup is not None else {}),
                    )
                    delivered_message_id = delivered.message_id
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
            "message_id=COALESCE($6,message_id),"
            "sent_at=CASE WHEN $6::bigint IS NOT NULL THEN now() ELSE sent_at END,"
            "next_attempt_at=now()+make_interval(secs=>$5) "
            "WHERE broadcast_id=$1 AND telegram_id=$2",
            row["broadcast_id"],
            row["telegram_id"],
            state,
            error,
            float(delay),
            delivered_message_id,
        )
        from bot.observability import current, Job
        journal = current()
        if journal:
            job = Job(f"broadcast:{row['broadcast_id']}:{row['telegram_id']}",
                      f"broadcast:{row['broadcast_id']}:{row['telegram_id']}",
                      row['telegram_id'],'broadcast','telegram','upload',
                      delivered=int(state=='sent'),reason=error or state)
            journal.event(job,state)
            if state in {'blocked','failed','uncertain'}:
                journal.incident(job,{'uncertain':'delivery_unknown'}.get(state,state),0)
            elif state == 'sent':
                journal.resolve(job.scope)
        await asyncio.sleep(1)


async def supervised_worker(bot: Bot, db: Database) -> None:
    while True:
        try:
            await run_worker(bot, db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Announcement worker failed; recovering in 10 seconds")
            try:
                await db.pool.execute(
                    "UPDATE broadcast_recipients SET state='uncertain' WHERE state='sending'"
                )
            except Exception:
                logger.warning("Announcement recovery database unavailable",exc_info=True)
            await asyncio.sleep(10)
