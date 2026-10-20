"""Application entrypoint: wires together config, database, bot and health server."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat

from bot.broadcasts import supervised_worker
from bot.config import Settings, load_settings
from bot.db import Database
from bot.handlers import create_root_router
from bot.handlers.download import set_instagram_watch
from bot.health import start_health_server
from bot.igsession import InstagramSession
from bot.operator import prepare
from bot.progress import CancelRegistry
from bot.proxy import configure_router
from bot.runtime import config
from bot.telegram_outbound import OutgoingLimiter
from bot.urlcache import UrlCache

logger = logging.getLogger(__name__)


async def _set_command_menus(bot: Bot, settings: Settings) -> None:
    """Public menu for everyone; the operator's chat gets the rest of it.

    The admin commands are scoped to one chat, so nobody else is even offered
    them. /restart used to sit in the public list, which advertised a command to
    every user that the router rejects for all of them - the only thing that
    reached them was the fallback's "I do not understand".
    """
    public = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="📖 Все возможности и примеры"),
        BotCommand(command="suggest", description="💡 Предложить идею или сайт"),
        BotCommand(command="contact", description="✉️ Связаться с командой"),
        BotCommand(command="end", description="Завершить диалог"),
    ]
    await bot.set_my_commands(public)
    try:
        await bot.set_my_short_description(
            short_description="Видео, фото и музыка по ссылке. Возможности и примеры — /help."
        )
        await bot.set_my_description(
            description=(
                "Скачивайте TikTok, YouTube, Instagram, Pinterest, X и музыку из "
                "SoundCloud, Spotify и Яндекс Музыки. Просто отправьте ссылку. "
                "Для видео: выбор качества, подрезка по времени и отправка частями. "
                "TikTok-медиа можно получить исходными файлами. Все возможности — /help."
            )
        )
    except TelegramAPIError as exc:
        logger.warning("Could not update public bot description: %s", exc)
    admin = [
        *public,
        BotCommand(command="stats", description="📈 Итоги за период"),
        BotCommand(command="dashboard", description="🖥 Защищённая панель статистики"),
        BotCommand(command="events", description="📊 Действия пользователей"),
        BotCommand(command="analytics", description="📊 Активность и надёжность"),
        BotCommand(command="analytics_export", description="📄 Выгрузка событий CSV"),
        BotCommand(command="users", description="👥 Найти пользователя"),
        BotCommand(command="tell", description="✉️ Написать пользователю"),
        BotCommand(command="broadcast", description="📣 Создать рассылку"),
        BotCommand(command="broadcasts", description="📣 Доставка и остановка рассылок"),
        BotCommand(command="suggestions", description="💡 Очередь предложений"),
        BotCommand(command="reply", description="✉️ Ответить на предложение"),
        BotCommand(command="post", description="📣 Новость в канал"),
        BotCommand(command="admin24", description="Статистика и выгрузки"),
        BotCommand(command="control", description="Панель управления"),
        BotCommand(command="vpn", description="🛡 Конфиги VPN"),
        BotCommand(command="iglogin", description="🔑 Войти в Instagram"),
        BotCommand(command="igcaptcha", description="🖥 Пройти капчу Instagram"),
        BotCommand(command="forget", description="🗑 Забыть ссылку в кэше"),
        BotCommand(command="restart", description="♻️ Перезапустить бота"),
    ]
    try:
        await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=settings.admin_id))
    except TelegramBadRequest as exc:
        # Happens only if the admin has never started a chat with the bot.
        logger.warning("Could not set the admin command menu: %s", exc)
    broadcast_chat = os.getenv("BROADCAST_CHAT_ID", "").strip()
    if broadcast_chat:
        try:
            await bot.set_my_commands([
                BotCommand(command="broadcast", description="📣 Черновик и предпросмотр"),
                BotCommand(command="broadcasts", description="📊 Доставка и остановка"),
            ], scope=BotCommandScopeChat(chat_id=int(broadcast_chat)))
        except (TelegramAPIError, ValueError) as exc:
            logger.warning("Could not set announcement chat commands: %s", exc)


async def run() -> None:
    settings = load_settings()

    db = Database(settings.database_dsn)
    await db.connect()
    await db.create_schema()
    await prepare(db)
    if settings.legacy_dump_file is not None:
        await db.seed_from_legacy_dump(settings.legacy_dump_file)

    await config.load(db)

    proxy_router = configure_router(
        settings.proxy_url,
        os.getenv("GOIDA_PROXY_URL") or None,
        os.getenv("BYPASS_PROXY_URL") or None,
    )
    await proxy_router.start()
    if proxy_router.enabled:
        logger.info("Adaptive VLESS routing enabled (fallback proxy: %s)", settings.proxy_url)

    # Media uploads set their own deadline from the file size and pass it through
    # the bot call, which is the only place aiogram honours one — the answer_*
    # shortcuts really do swallow a request_timeout, as the note here used to
    # say. What is left for this default is every other call, and ten minutes was
    # far too patient for those: a failing edit of a progress bar sat for the
    # whole of it, so the bar froze while the download underneath was still fine.
    # Long polling is unaffected — aiogram asks getUpdates for this value plus the
    # polling wait, so it stays comfortably above the 30s it needs.
    session_kwargs: dict = {"timeout": 90}
    if settings.api_base_url:
        # Self-hosted telegram-bot-api server: raises the upload limit to 2 GB.
        session_kwargs["api"] = TelegramAPIServer.from_base(settings.api_base_url)
        logger.info("Using local Bot API server at %s", settings.api_base_url)
    session = AiohttpSession(**session_kwargs)
    session.middleware(OutgoingLimiter())
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

    # The account has been renewed by hand three times, each time after users
    # noticed before the bot did. This asks Instagram who it is signed in as and
    # signs back in on its own; see bot/igsession.py for why it asks rather than
    # infers, and why acting on a wrong answer is cheap.
    async def _tell_admin(text: str, kind: str | None = None) -> None:
        markup = None
        if kind == "captcha":
            # One tap from the notice to a link, so a CAPTCHA that appears at
            # night costs a tap in the morning rather than a command to recall.
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="\U0001f5a5 Пройти капчу", callback_data="igcaptcha",
            )]])
        await bot.send_message(settings.admin_id, text, reply_markup=markup)

    ig_session = InstagramSession(
        settings.cookies_file,
        Path(os.getenv("IG_REQUEST_FILE", "data/iglogin_request")),
        Path(os.getenv("IG_RESULT_FILE", "data/iglogin_result.txt")),
        _tell_admin,
        enabled=os.getenv("IG_AUTOLOGIN", "1") not in ("0", "false", "no"),
    )
    dispatcher["ig_session"] = ig_session
    set_instagram_watch(ig_session)
    from bot import igbrowser

    igbrowser.set_notifier(_tell_admin)
    await ig_session.start()

    health_runner = await start_health_server(settings.health_port, db, settings.admin_id)
    with suppress(Exception):
        await db.record_event(settings.admin_id, "system:startup", "ok")
    announcement_worker = asyncio.create_task(supervised_worker(bot, db))

    async def retain_events() -> None:
        while True:
            try:
                await db.pool.execute(
                    "DELETE FROM user_events WHERE id IN (SELECT id FROM user_events "
                    "WHERE created_at < now()-interval '90 days' ORDER BY created_at LIMIT 1000)"
                )
                await db.pool.execute(
                    "DELETE FROM support_messages WHERE (chat_id,message_id) IN "
                    "(SELECT chat_id,message_id FROM support_messages WHERE "
                    "created_at < now()-interval '90 days' ORDER BY created_at LIMIT 1000)"
                )
            except Exception:
                logger.exception("Event retention failed; retrying next hour")
            await asyncio.sleep(3600)

    retention_worker = asyncio.create_task(retain_events())
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
        with suppress(Exception):
            await db.record_event(settings.admin_id, "system:shutdown", "ok")
        for task in (announcement_worker, retention_worker):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await ig_session.stop()
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
