"""Basic user commands and the catch-all fallback."""

from __future__ import annotations

import os
from contextlib import suppress

from aiogram import Router, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot.db import Database

router = Router(name="user")
fallback_router = Router(name="fallback")

_HELP_TEXT = "Отправьте ссылку на видео из TikTok, YouTube или Instagram."


@router.message(CommandStart())
async def handle_start(message: Message, db: Database) -> None:
    await db.upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Привет, {html.quote(message.from_user.full_name)}!\n\n"
        "Это загрузчик видео из TikTok (без водяных знаков), YouTube и Instagram.\n\n"
        f"{_HELP_TEXT}"
    )


@router.message(Command("restart"))
async def handle_restart(message: Message, db: Database) -> None:
    await db.upsert_user(message.from_user.id, message.from_user.username)
    with suppress(TelegramBadRequest):
        await message.delete()
    await message.answer(f"Бот успешно перезапущен!🔄\n\n{_HELP_TEXT}")


@router.message(Command("suggest"))
async def handle_suggest(message: Message, db: Database, command: CommandObject) -> None:
    """Forward a user's idea/site request to the admin's DM."""
    await db.upsert_user(message.from_user.id, message.from_user.username)
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "💡 <b>Предложка</b>\n\nНапишите идею или сайт сразу после команды, например:\n"
            "<code>/suggest добавьте скачивание с Pinterest</code>"
        )
        return
    admin_id = os.getenv("ADMIN_ID", "").strip()
    if admin_id.isdigit():
        u = message.from_user
        who = f"@{u.username}" if u.username else html.quote(u.full_name)
        with suppress(Exception):
            await message.bot.send_message(
                int(admin_id),
                f"💡 <b>Новое предложение</b>\nОт: {who} (id <code>{u.id}</code>)\n\n"
                f"{html.quote(text)}",
            )
    await message.answer("Спасибо! 🙌 Предложение отправлено — лучшие идеи добавим в бота.")


@fallback_router.message()
async def handle_unknown(message: Message) -> None:
    await message.answer(f"Я не понимаю.\n\n{_HELP_TEXT}")
