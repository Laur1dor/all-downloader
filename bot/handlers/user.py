"""Basic user commands and the catch-all fallback."""

from __future__ import annotations

import logging
import os

from aiogram import F, Router, html
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.db import Database

router = Router(name="user")
fallback_router = Router(name="fallback")
logger = logging.getLogger(__name__)

_NEWS_URL = "https://t.me/TikTokDownloaderFF_news"
_HELP_TEXT = "Пришлите ссылку на пост или видео. Возможности — /help, идеи — /suggest."

_HELP_PAGES = {
    "index": (
        "<b>Что умеет бот</b>\n\n"
        "Отправьте ссылку на отдельный пост, ролик или трек. "
        "Бот сам определит сервис и пришлёт медиа сюда.\n\n"
        "Выберите раздел ниже — там сервисы, примеры и ограничения."
    ),
    "video": (
        "🎬 <b>Видео и фото</b>\n\n"
        '• <a href="https://www.tiktok.com/">TikTok</a> — видео, фото и карусели; '
        "кнопка «Файл» для исходных медиа. Описание и музыка — если есть.\n"
        '• <a href="https://www.youtube.com/">YouTube</a> — видео и Shorts; '
        "для обычного ролика доступен выбор качества или аудио.\n"
        '• <a href="https://www.instagram.com/">Instagram</a> — Reels, посты, карусели.\n'
        '• <a href="https://www.pinterest.com/">Pinterest</a> — отдельные пины '
        "(доски и профили не поддерживаются).\n"
        '• <a href="https://x.com/">X / Twitter</a> — медиа из постов.\n'
        '• <a href="https://www.pornhub.com/">Pornhub</a>, '
        '<a href="https://rule34video.com/">Rule34Video</a>, '
        '<a href="https://rule34.xxx/">Rule34</a> и '
        '<a href="https://the-joi-database.com/">the-joi-database</a> — '
        "ссылки на отдельные материалы; доступность зависит от сайта."
    ),
    "music": (
        "🎵 <b>Музыка</b>\n\n"
        '• <a href="https://music.youtube.com/">YouTube Music</a> — аудио из трека.\n'
        '• <a href="https://soundcloud.com/">SoundCloud</a> — отдельный трек.\n'
        '• <a href="https://open.spotify.com/">Spotify</a> — отдельный трек; '
        "полная версия зависит от доступного источника.\n"
        '• <a href="https://music.yandex.ru/">Яндекс Музыка</a> — отдельный трек; '
        "если полная версия недоступна, бот может прислать официальное превью.\n\n"
        "Альбомы и плейлисты не поддерживаются."
    ),
    "parts": (
        "✂️ <b>Подрезка и части</b>\n\n"
        "Пришлите ссылку и интервал через пробел:\n"
        "<code>https://www.youtube.com/watch?v=... 01:20-02:40</code>\n\n"
        "Если видео не помещается в один файл Telegram, бот подготовит "
        "отдельно воспроизводимые части. Для очень длинных или больших роликов "
        "действуют ограничения; бот сообщит, если обработать их не удалось."
    ),
    "contact": (
        "✉️ <b>Связь и новости</b>\n\n"
        "<code>/contact</code> — написать команде: после команды пришлите "
        "сообщение, фото или файл. Ответ придёт в этот чат.\n"
        "<code>/end</code> — завершить диалог и вернуться к загрузкам.\n"
        "<code>/suggest ваша идея</code> — предложить сайт или функцию.\n\n"
        f'📣 <a href="{_NEWS_URL}">Канал новостей</a>.'
    ),
}

_HELP_SECTIONS = (
    ("🎬 Видео и фото", "video"),
    ("🎵 Музыка", "music"),
    ("✂️ Подрезка и части", "parts"),
    ("✉️ Связь", "contact"),
)


def _help_keyboard(page: str = "index") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"help:{key}")]
        for title, key in _HELP_SECTIONS if key != page
    ]
    if page != "index":
        rows.append([InlineKeyboardButton(text="← Содержание", callback_data="help:index")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def handle_start(message: Message, db: Database) -> None:
    await db.upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Привет, {html.quote(message.from_user.full_name)}!\n\n"
        "TikTok, YouTube, Instagram, Pinterest, X и музыка — по ссылке. "
        "Видео, фото и аудио придут прямо сюда.\n\n"
        f"{_HELP_TEXT}"
    )


@router.message(Command("help"))
async def handle_help(message: Message, db: Database) -> None:
    await db.upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        _HELP_PAGES["index"], reply_markup=_help_keyboard(), disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("help:"))
async def show_help_section(callback: CallbackQuery) -> None:
    page = (callback.data or "").removeprefix("help:")
    if page not in _HELP_PAGES or not isinstance(callback.message, Message):
        await callback.answer("Раздел недоступен.")
        return
    await callback.answer()
    try:
        await callback.message.edit_text(
            _HELP_PAGES[page], reply_markup=_help_keyboard(page),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        # A second tap on an unchanged page needs no replacement message.
        pass


@router.message(Command("suggest"))
async def handle_suggest(message: Message, db: Database, command: CommandObject) -> None:
    """Keep requests in a durable inbox and notify its dedicated chat."""
    await db.upsert_user(message.from_user.id, message.from_user.username)
    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "💡 <b>Предложка</b>\n\nНапишите идею или сайт сразу после команды, например:\n"
            "<code>/suggest добавьте скачивание с Pinterest</code>"
        )
        return
    if len(text) > 3500:
        await message.answer("Предложение слишком длинное. Сократите до 3500 символов.")
        return
    u = message.from_user
    suggestion_id = await db.add_suggestion(u.id, u.username, text)
    inbox = os.getenv("SUGGESTIONS_CHAT_ID", "").strip() or os.getenv("ADMIN_ID", "").strip()
    if inbox:
        who = f"@{u.username}" if u.username else html.quote(u.full_name)
        try:
            await message.bot.send_message(
                inbox,
                f"💡 <b>Предложение #{suggestion_id}</b>\nОт: {who}"
                f" (id <code>{u.id}</code>)\n\n{html.quote(text)}\n\n"
                f"Ответ: <code>/reply {suggestion_id} ваш текст</code>",
            )
        except Exception:
            logger.warning("Could not notify suggestion inbox", exc_info=True)
    await message.answer(
        f"Спасибо! Предложение #{suggestion_id} сохранено. Ответ придёт сюда."
    )


@fallback_router.message(F.chat.type == "private")
async def handle_unknown(message: Message) -> None:
    await message.answer(f"Я не понимаю.\n\n{_HELP_TEXT}")
