"""Application entrypoint: wires together config, database, bot and health server."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat

from bot.config import Settings, load_settings
from bot.db import Database
from bot.handlers import create_root_router
from bot.health import start_health_server
from bot.progress import CancelRegistry
from bot.proxy import configure_router
from bot.runtime import config
from bot.urlcache import UrlCache

logger = logging.getLogger(__name__)


async def _set_command_menus(bot: Bot, settings: Settings) -> None:
    """Public menu for everyone; the admin's chat additionally shows /admin24."""
    public = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="restart", description="Перезапустить бота"),
    ]
    await bot.set_my_commands(public)
    admin = [
        *public,
        BotCommand(command="admin24", description="Статистика бота"),
        BotCommand(command="control", description="Панель управления"),
    ]
    try:
        await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=settings.admin_id))
    except TelegramBadRequest as exc:
        # Happens only if the admin has never started a chat with the bot.
        logger.warning("Could not set the admin command menu: %s", exc)


async def run() -> None:
    settings = load_settings()

    db = Database(settings.database_dsn)
    await db.connect()
    await db.create_schema()
    if settings.legacy_dump_file is not None:
        await db.seed_from_legacy_dump(settings.legacy_dump_file)

    await config.load(db)

    proxy_router = configure_router(settings.proxy_url)
    await proxy_router.start()
    if proxy_router.enabled:
        logger.info("Adaptive VLESS routing enabled (fallback proxy: %s)", settings.proxy_url)

    # Big uploads take minutes; the default 60s session timeout is not enough.
    # (Message.answer_* shortcuts can't override the timeout per call.)
    session_kwargs: dict = {"timeout": 600}
    if settings.api_base_url:
        # Self-hosted telegram-bot-api server: raises the upload limit to 2 GB.
        session_kwargs["api"] = TelegramAPIServer.from_base(settings.api_base_url)
        logger.info("Using local Bot API server at %s", settings.api_base_url)
    session = AiohttpSession(**session_kwargs)
    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(
        db=db,
        settings=settings,
        url_cache=UrlCache(),
        cancel_registry=CancelRegistry(),
    )
    dispatcher.include_router(create_root_router(settings.admin_id))

    health_runner = await start_health_server(settings.health_port)
    try:
        await _set_command_menus(bot, settings)
        try:
            # Lets the admin see that /restart (or a redeploy) actually worked.
            await bot.send_message(settings.admin_id, "✅ Бот запущен и готов к работе.")
        except TelegramAPIError as exc:
            logger.warning("Could not notify the admin about startup: %s", exc)
        logger.info("Starting polling")
        await dispatcher.start_polling(bot)
    finally:
        await proxy_router.stop()
        await health_runner.cleanup()
        await db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
