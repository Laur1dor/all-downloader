"""Durable operator conversations and announcement delivery state."""

from __future__ import annotations

from bot.db import Database

SCHEMA = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE;
CREATE TABLE IF NOT EXISTS support_sessions (
 telegram_id BIGINT PRIMARY KEY, opened_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS support_messages (
 chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, telegram_id BIGINT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(chat_id,message_id)
);
CREATE TABLE IF NOT EXISTS broadcasts (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_chat BIGINT NOT NULL, source_message BIGINT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS broadcast_recipients (
 broadcast_id BIGINT NOT NULL REFERENCES broadcasts(id), telegram_id BIGINT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), error TEXT,
 PRIMARY KEY(broadcast_id,telegram_id)
);
CREATE INDEX IF NOT EXISTS broadcast_pending ON broadcast_recipients(state,next_attempt_at);
-- Restore former opt-outs once. Keep Telegram-forbidden recipients disabled;
-- notifications_enabled now records delivery availability, not a preference.
WITH applied AS (
 INSERT INTO settings(key,value) VALUES('broadcast_all_registered_v1','1')
 ON CONFLICT(key) DO NOTHING RETURNING key
)
UPDATE users u SET notifications_enabled=TRUE
WHERE NOT notifications_enabled AND EXISTS (SELECT 1 FROM applied)
AND NOT EXISTS (
 SELECT 1 FROM broadcast_recipients r WHERE r.telegram_id=u.telegram_id AND r.state='blocked'
);
"""


async def prepare(db: Database) -> None:
    await db.pool.execute(SCHEMA)
    # A process could have died after Telegram accepted a message. Never replay
    # these automatically: delivery cannot be determined from a Bot API timeout.
    await db.pool.execute("UPDATE broadcast_recipients SET state='uncertain' WHERE state='sending'")


async def remember(db: Database, chat_id: int, message_id: int, user_id: int) -> None:
    await db.pool.execute(
        "INSERT INTO support_messages(chat_id,message_id,telegram_id) VALUES($1,$2,$3) "
        "ON CONFLICT DO NOTHING",
        chat_id,
        message_id,
        user_id,
    )


async def recipient(db: Database, reference: str) -> int | None:
    if reference.isdigit():
        return await db.pool.fetchval(
            "SELECT telegram_id FROM users WHERE telegram_id=$1", int(reference)
        )
    return await db.pool.fetchval(
        "SELECT telegram_id FROM users WHERE lower(username)=lower($1)", reference.lstrip("@")
    )
