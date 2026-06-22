"""Basic user commands and the catch-all fallback."""

from __future__ import annotations

from contextlib import suppress

from aiogram import Router, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
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


@fallback_router.message()
async def handle_unknown(message: Message) -> None:
    await message.answer(f"Я не понимаю.\n\n{_HELP_TEXT}")
