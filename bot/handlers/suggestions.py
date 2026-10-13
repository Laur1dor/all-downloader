"""Operator inbox, direct replies and a manually curated news channel."""

from __future__ import annotations

import logging
import os

from aiogram import F, Router, html
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message

from bot.db import Database
from bot.operator import remember

logger = logging.getLogger(__name__)


def create_admin_router(admin_id: int) -> Router:
    router = Router(name="suggestions_admin")
    router.message.filter(F.from_user.id == admin_id)

    @router.message(Command("suggestions"))
    async def list_suggestions(message: Message, db: Database) -> None:
        rows = await db.pending_suggestions()
        if not rows:
            await message.answer("В предложке пока пусто.")
            return
        for row in rows:
            author = html.quote(row["username"] or str(row["telegram_id"]))
            await message.answer(
                f"💡 <b>#{row['id']}</b> · {author}\n"
                f"{html.quote(row['body'][:3000])}\n\n"
                f"Ответ: <code>/reply {row['id']} ваш текст</code>"
            )

    @router.message(Command("reply"))
    async def reply_suggestion(message: Message, db: Database) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: <code>/reply 123 текст ответа</code>")
            return
        suggestion_id = int(parts[1])
        answer = parts[2].strip()[:3500]
        row = await db.get_suggestion(suggestion_id)
        if row is None or row["answered_at"] is not None:
            await message.answer("Предложение не найдено или уже закрыто.")
            return
        try:
            sent = await message.bot.send_message(
                row["telegram_id"],
                f"💡 <b>Ответ на предложение #{suggestion_id}</b>\n\n{html.quote(answer)}"
                "\n\nОтветьте на это сообщение или напишите /contact.",
            )
            await remember(db, row["telegram_id"], sent.message_id, row["telegram_id"])
        except TelegramAPIError as exc:
            logger.warning("Could not reply to suggestion %s: %s", suggestion_id, exc)
            await message.answer("Не удалось доставить ответ. Предложение осталось открытым.")
            return
        await db.answer_suggestion(suggestion_id, answer)
        await message.answer(f"Ответ на предложение #{suggestion_id} отправлен.")

    @router.message(Command("events"))
    async def list_events(message: Message, db: Database) -> None:
        rows = await db.event_summary()
        lines = ["📊 <b>Действия за 24 часа</b>"]
        lines.extend(
            f"{html.quote(row['action'])} · {html.quote(row['outcome'])}: {row['total']}"
            for row in rows
        )
        await message.answer("\n".join(lines))

    @router.message(Command("post"))
    async def post_news(message: Message) -> None:
        channel_id = os.getenv("NEWS_CHANNEL_ID", "").strip()
        body = (message.text or "").partition(" ")[2].strip()
        if not channel_id or not (body or message.reply_to_message):
            await message.answer(
                "Ответьте командой /post на оформленную новость: текст, фото или файл. "
                "Либо напишите <code>/post текст новости</code>. Нужен NEWS_CHANNEL_ID."
            )
            return
        try:
            if message.reply_to_message:
                source = message.reply_to_message
                await message.bot.copy_message(channel_id, source.chat.id, source.message_id)
            else:
                await message.bot.send_message(channel_id, html.quote(body[:4000]))
        except TelegramAPIError as exc:
            logger.warning("Could not post to news channel: %s", exc)
            await message.answer("Публикация не прошла. Проверьте ID канала и права бота.")
            return
        await message.answer("Новость опубликована.")

    return router
