"""Publish the approved service guide with a short pinned navigation post."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest

from bot.config import load_settings
from bot.db import Database
from bot.telegram_outbound import OutgoingLimiter


async def main() -> None:
    settings = load_settings()
    channel = int(os.environ["NEWS_CHANNEL_ID"])
    posts = re.findall(r"```html\s*\n(.*?)\n```", Path(sys.argv[1]).read_text("utf-8"), re.S)
    if len(posts) != 4 or any(len(post) > 4096 for post in posts):
        raise ValueError("Expected four Telegram-sized HTML guide posts")
    kwargs = {"timeout": 90}
    if settings.api_base_url:
        kwargs["api"] = TelegramAPIServer.from_base(settings.api_base_url)
    session = AiohttpSession(**kwargs)
    session.middleware(OutgoingLimiter())
    bot = Bot(settings.bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    db = Database(settings.database_dsn)
    await db.connect()
    try:
        saved = await db.pool.fetchval("SELECT value FROM settings WHERE key='channel_guide_v2'")
        state = json.loads(saved) if saved else {"posts": [], "index": 7}

        async def checkpoint() -> None:
            await db.pool.execute(
                "INSERT INTO settings(key,value) VALUES('channel_guide_v2',$1) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                json.dumps(state),
            )

        for index, text in enumerate(posts):
            if index < len(state["posts"]):
                try:
                    await bot.edit_message_text(
                        text, chat_id=channel, message_id=state["posts"][index],
                        disable_web_page_preview=True,
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc):
                        raise
                continue
            message = await bot.send_message(
                channel, text, disable_web_page_preview=True, disable_notification=True
            )
            state["posts"].append(message.message_id)
            await checkpoint()
        labels = [
            "Как пользоваться",
            "Видео и фото · все сайты",
            "Музыка",
            "Подрезка, части и связь",
        ]
        navigation = "📌 <b>Возможности @TikTokDownloaderFF_bot</b>\n\n"
        navigation += "Просто отправьте боту ссылку. Краткая справка прямо в боте: /help.\n\n"
        navigation += "\n".join(
            f'<a href="https://t.me/TikTokDownloaderFF_news/{mid}">{label}</a>'
            for label, mid in zip(labels, state["posts"], strict=True)
        )
        navigation += "\n\nНовости и известные ограничения — в этом канале. Идеи: /suggest."
        try:
            await bot.edit_message_text(
                navigation,
                chat_id=channel,
                message_id=state["index"],
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                message = await bot.send_message(
                    channel, navigation, disable_web_page_preview=True, disable_notification=True
                )
                state["index"] = message.message_id
                await checkpoint()
        pinned = True
        try:
            await bot.pin_chat_message(channel, state["index"], disable_notification=True)
        except TelegramBadRequest:
            pinned = False
        print(json.dumps({"channel": channel, "pinned": pinned, **state}))
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
