"""Private user conversations relayed through the operator inbox."""

from __future__ import annotations

import os

from aiogram import F, Router, html
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.db import Database
from bot.operator import recipient, remember


def create_router(admin_id: int) -> Router:
    router = Router(name="support")
    inbox = int(os.getenv("SUGGESTIONS_CHAT_ID") or admin_id)

    async def deliver(message: Message, db: Database, user_id: int, text: str = "") -> None:
        try:
            if text:
                sent = await message.bot.send_message(
                    user_id,
                    "✉️ <b>Сообщение от команды бота</b>\n\n"
                    + html.quote(text)
                    + "\n\nОтветьте на это сообщение или напишите /contact.",
                )
            else:
                sent = await message.bot.copy_message(user_id, message.chat.id, message.message_id)
                await message.bot.send_message(
                    user_id, "✉️ Сообщение от команды бота. Ответить: /contact."
                )
            await remember(db, user_id, sent.message_id, user_id)
            await message.answer("Сообщение доставлено.")
        except TelegramAPIError:
            await message.answer(
                "Не удалось доставить сообщение: пользователь мог заблокировать бота."
            )

    @router.message(Command("users"), F.from_user.id == admin_id)
    async def users(message: Message, db: Database, command: CommandObject) -> None:
        query = (command.args or "").strip().lstrip("@")[:100]
        rows = await db.pool.fetch(
            "SELECT telegram_id,username FROM users WHERE $1='' OR "
            "strpos(lower(coalesce(username,'')),lower($1))>0 OR telegram_id::text=$1 "
            "ORDER BY registration_date DESC LIMIT 20",
            query,
        )
        lines = ["👥 <b>Пользователи</b> · последние 20 совпадений"]
        lines += [
            f"<code>{r['telegram_id']}</code> · {html.quote(r['username'] or 'без username')}"
            for r in rows
        ]
        lines.append(
            "\nНаписать: <code>/tell ID текст</code> или ответьте командой "
            "<code>/tell ID</code> на готовое сообщение."
        )
        await message.answer("\n".join(lines))

    @router.message(Command("tell"), F.from_user.id == admin_id)
    async def tell(message: Message, db: Database, command: CommandObject) -> None:
        parts = (command.args or "").split(maxsplit=1)
        if not parts:
            await message.answer(
                "<code>/tell ID|@username текст</code> · пользователь должен запускать бота."
            )
            return
        user_id = await recipient(db, parts[0])
        if user_id is None:
            await message.answer("Пользователь не найден в базе бота.")
            return
        if len(parts) == 2:
            await deliver(message, db, user_id, parts[1][:3500])
        elif message.reply_to_message:
            await deliver(message.reply_to_message, db, user_id)
        else:
            await message.answer(
                "Добавьте текст или ответьте на сообщение, которое нужно отправить."
            )

    @router.message(Command("contact"), F.chat.type == "private")
    async def contact(message: Message, db: Database) -> None:
        await db.upsert_user(message.from_user.id, message.from_user.username)
        await db.pool.execute(
            "INSERT INTO support_sessions(telegram_id) VALUES($1) ON CONFLICT DO NOTHING",
            message.from_user.id,
        )
        await message.answer(
            "✉️ <b>Связь с командой бота</b>\n\nПришлите сообщение, фото или файл. "
            "Ответ придёт сюда. Завершить диалог и вернуться к загрузкам: /end."
        )

    @router.message(Command("end"), F.chat.type == "private")
    async def end(message: Message, db: Database) -> None:
        await db.pool.execute(
            "DELETE FROM support_sessions WHERE telegram_id=$1", message.from_user.id
        )
        await message.answer("Диалог завершён. Можно снова присылать ссылки для скачивания.")

    async def is_support(message: Message, db: Database) -> bool:
        if not message.from_user or (message.text or "").startswith("/"):
            return False
        if message.from_user.id == admin_id:
            return bool(
                message.reply_to_message
                and message.chat.id in (inbox, admin_id)
                and await db.pool.fetchval(
                    "SELECT telegram_id FROM support_messages WHERE chat_id=$1 AND message_id=$2",
                    message.chat.id,
                    message.reply_to_message.message_id,
                )
            )
        if message.chat.type != "private":
            return False
        return bool(
            await db.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM support_sessions WHERE telegram_id=$1) OR "
                "EXISTS(SELECT 1 FROM support_messages WHERE chat_id=$1 AND message_id=$2)",
                message.from_user.id,
                message.reply_to_message.message_id if message.reply_to_message else 0,
            )
        )

    @router.message(is_support)
    async def relay(message: Message, db: Database) -> None:
        if message.from_user.id == admin_id:
            user_id = await db.pool.fetchval(
                "SELECT telegram_id FROM support_messages WHERE chat_id=$1 AND message_id=$2",
                message.chat.id,
                message.reply_to_message.message_id,
            )
            await deliver(message, db, user_id)
            return
        user = message.from_user
        try:
            header = await message.bot.send_message(
                inbox,
                f"✉️ <b>Диалог</b> · <code>{user.id}</code> · "
                f"{html.quote('@' + user.username if user.username else user.full_name)}\n"
                "Ответьте на это сообщение или на вложение ниже.",
            )
            await remember(db, inbox, header.message_id, user.id)
            copied = await message.bot.copy_message(inbox, message.chat.id, message.message_id)
            await remember(db, inbox, copied.message_id, user.id)
            await message.answer("Передано команде бота.")
        except TelegramAPIError:
            await message.answer("Не удалось передать сообщение. Попробуйте позже.")

    return router
