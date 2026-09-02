"""Admin commands: statistics and a real bot restart.

The router-level filter rejects everyone except ADMIN_ID on the server side:
for any other user these commands fall through to the regular handlers as if
they did not exist, so they cannot be discovered or bypassed via callbacks.
"""

from __future__ import annotations

import html
import logging
import signal
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import vpnstore
from bot.db import Database, hash_url
from bot.urlkey import canonical_key, is_short_link
from bot.runtime import (
    BANDWIDTH_OPTIONS_MBIT,
    RANGES,
    SPEED_OPTIONS_MBIT,
    UPLOAD_OPTIONS_MB,
    config,
)

logger = logging.getLogger(__name__)

_CONTROL_PREFIX = "ctl:"
# An escaped newline inside a string is one of the easier things to mangle
# while editing; a named character sidesteps the question.
NEWLINE = chr(10)

# admin_id -> setting key awaiting a typed custom value.
_awaiting: dict[int, str] = {}
# Admins whose next message is a VPN config rather than a link to download.
# Armed by a button so an ordinary message is never mistaken for one.
_awaiting_vpn: dict[int, bool] = {}
# Human labels for the custom-input prompt.
_SETTING_LABELS = {
    "tiktok_route": "маршрут TikTok",
    "main_exit": "движок своего VPN",
    "user_upload_mb": "лимит загрузки юзеров (МБ)",
    "admin_upload_mb": "лимит загрузки админа (МБ)",
    "user_speed_mbit": "скорость юзеров (Мбит/с, 0 = безлимит)",
    "admin_speed_mbit": "скорость админа (Мбит/с, 0 = безлимит)",
    "vpn_user_speed_mbit": "скорость юзеров через VPN (Мбит/с, 0 = безлимит)",
    "vpn_admin_speed_mbit": "скорость админа через VPN (Мбит/с, 0 = безлимит)",
    "bandwidth_mbit": "общий канал (Мбит/с, 0 = безлимит)",
}


def _mb_label(mb: int) -> str:
    return f"{mb // 1000} ГБ" if mb >= 1000 else f"{mb} МБ"


def _option_row(key: str, options: tuple[int, ...], current: int, fmt) -> list:
    row = []
    for value in options:
        mark = "✅ " if value == current else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{fmt(value)}",
                callback_data=f"{_CONTROL_PREFIX}{key}:{value}",
            )
        )
    # A custom value gets highlighted on the ✏️ button itself.
    custom_mark = "✅ " if current not in options else ""
    row.append(
        InlineKeyboardButton(
            text=f"{custom_mark}✏️", callback_data=f"{_CONTROL_PREFIX}custom:{key}"
        )
    )
    return row


def _speed_label(mbit: int) -> str:
    return "∞" if mbit == 0 else f"{mbit}"


def _speed_line(key: str) -> str:
    v = config.get(key)
    return f"{v} Мбит/с" if v else "∞"


def _control_text() -> str:
    return (
        "⚙️ <b>Управление</b>\n\n"
        f"📦 Лимит загрузки — юзеры: <b>{_mb_label(config.get('user_upload_mb'))}</b>,"
        f" админ: <b>{_mb_label(config.get('admin_upload_mb'))}</b>\n"
        f"🚦 Скорость — юзеры: <b>{_speed_line('user_speed_mbit')}</b>,"
        f" админ: <b>{_speed_line('admin_speed_mbit')}</b>\n"
        f"🛡 Через VPN — юзеры: <b>{_speed_line('vpn_user_speed_mbit')}</b>,"
        f" админ: <b>{_speed_line('vpn_admin_speed_mbit')}</b>\n"
        f"📶 Канал (всего): <b>{_speed_line('bandwidth_mbit')}</b>\n"
        f"🔒 Solo-режим (пауза для юзеров): <b>{'ВКЛ' if config.solo_mode else 'выкл'}</b>\n\n"
        "<i>✏️ — задать своё значение в диапазоне.</i>"
    )


def _header(text: str) -> list:
    return [InlineKeyboardButton(text=text, callback_data=f"{_CONTROL_PREFIX}nop")]


def _control_keyboard() -> InlineKeyboardMarkup:
    rows = [
        _header("— 📦 Лимит юзеров (МБ) —"),
        _option_row("user_upload_mb", UPLOAD_OPTIONS_MB, config.get("user_upload_mb"), _mb_label),
        _header("— 📦 Лимит админа (МБ) —"),
        _option_row("admin_upload_mb", UPLOAD_OPTIONS_MB, config.get("admin_upload_mb"), _mb_label),
        _header("— 🚦 Скорость юзеров (Мбит/с) —"),
        _option_row("user_speed_mbit", SPEED_OPTIONS_MBIT, config.get("user_speed_mbit"),
                    _speed_label),
        _header("— 🚦 Скорость админа (Мбит/с) —"),
        _option_row("admin_speed_mbit", SPEED_OPTIONS_MBIT, config.get("admin_speed_mbit"),
                    _speed_label),
        _header("— 🛡 Через VPN: юзеры (Мбит/с) —"),
        _option_row("vpn_user_speed_mbit", SPEED_OPTIONS_MBIT, config.get("vpn_user_speed_mbit"),
                    _speed_label),
        _header("— 🛡 Через VPN: админ (Мбит/с) —"),
        _option_row("vpn_admin_speed_mbit", SPEED_OPTIONS_MBIT, config.get("vpn_admin_speed_mbit"),
                    _speed_label),
        _header("— 📶 Канал всего (Мбит/с) —"),
        _option_row("bandwidth_mbit", BANDWIDTH_OPTIONS_MBIT, config.get("bandwidth_mbit"),
                    _speed_label),
        _header("— 🛡 Свой VPN идёт через —"),
        [
            InlineKeyboardButton(
                text=("• xray •" if config.get("main_exit") == 0 else "xray"),
                callback_data=f"{_CONTROL_PREFIX}main_exit:0",
            ),
            InlineKeyboardButton(
                text=("• AmneziaWG •" if config.get("main_exit") == 1 else "AmneziaWG"),
                callback_data=f"{_CONTROL_PREFIX}main_exit:1",
            ),
            InlineKeyboardButton(
                text=("• sing-box •" if config.get("main_exit") == 2 else "sing-box"),
                callback_data=f"{_CONTROL_PREFIX}main_exit:2",
            ),
        ],
        _header("— 🎬 TikTok через —"),
        [
            InlineKeyboardButton(
                text=("• Гойда (пул) •" if not config.tiktok_via_own_vpn
                      else "Гойда (пул)"),
                callback_data=f"{_CONTROL_PREFIX}tiktok_route:0",
            ),
            InlineKeyboardButton(
                text=("• Свой VPN •" if config.tiktok_via_own_vpn else "Свой VPN"),
                callback_data=f"{_CONTROL_PREFIX}tiktok_route:1",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔒 Solo: ВЫКЛЮЧИТЬ" if config.solo_mode else "🔓 Solo: ВКЛЮЧИТЬ",
                callback_data=f"{_CONTROL_PREFIX}solo_mode:{0 if config.solo_mode else 1}",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_router(admin_id: int) -> Router:
    router = Router(name="admin")
    router.message.filter(F.from_user.id == admin_id)
    router.callback_query.filter(F.from_user.id == admin_id)

    @router.message(Command("admin24"))
    async def handle_admin_stats(message: Message, db: Database) -> None:
        stats = await db.fetch_summary()
        await message.answer(
            "📊 <b>Статистика бота</b>\n\n"
            f"👥 Пользователей: <b>{stats.total_users}</b> (+{stats.new_users_7d} за 7 дней)\n"
            f"🎬 Конвертаций: <b>{stats.total_conversions}</b>\n"
            f"✅ Успешных: {stats.done}\n"
            f"❌ Ошибок: {stats.failed}\n"
            f"🕒 За последние 24 часа: {stats.conversions_24h}"
        )
        # Two separate HTML files (full, sorted by date) — easier to open inline
        # on mobile than a zip. Data accumulates in PostgreSQL; nothing is dropped.
        users_report = await db.export_users_html()
        await message.answer_document(
            BufferedInputFile(users_report.encode("utf-8"), filename="users.html"),
            request_timeout=120,
        )
        conversions_report = await db.export_conversions_html()
        await message.answer_document(
            BufferedInputFile(conversions_report.encode("utf-8"), filename="conversions.html"),
            request_timeout=120,
        )

    @router.message(Command("control"))
    async def handle_control(message: Message) -> None:
        await message.answer(_control_text(), reply_markup=_control_keyboard())

    async def _redraw(message: Message) -> None:
        with suppress(TelegramBadRequest):
            await message.edit_text(_control_text(), reply_markup=_control_keyboard())

    @router.callback_query(F.data.startswith(_CONTROL_PREFIX))
    async def handle_control_button(callback: CallbackQuery) -> None:
        payload = callback.data.removeprefix(_CONTROL_PREFIX)
        if payload == "nop":
            await callback.answer()
            return
        key, _, raw = payload.partition(":")
        if key == "vpn":
            _awaiting_vpn[admin_id] = raw == "replace"
            await callback.answer()
            await callback.message.answer(
                "Пришлите конфиг следующим сообщением "
                + ("(заменю текущий)." if raw == "replace" else "(добавлю к текущим).")
            )
            return
        if key == "custom":
            if raw not in RANGES:
                await callback.answer()
                return
            _awaiting[admin_id] = raw
            low, high = RANGES[raw]
            await callback.answer()
            await callback.message.answer(
                f"✏️ Пришлите число для «{_SETTING_LABELS[raw]}» "
                f"(от {low} до {high}):"
            )
            return
        try:
            await config.set(key, int(raw))
        except (KeyError, ValueError):
            await callback.answer("Неизвестная настройка")
            return
        await callback.answer("Сохранено ✅")
        if isinstance(callback.message, Message):
            await _redraw(callback.message)

    @router.message(F.text.regexp(r"^-?\d+$"), lambda m: m.from_user.id in _awaiting)
    async def handle_custom_value(message: Message) -> None:
        key = _awaiting[admin_id]
        low, high = RANGES[key]
        value = int(message.text)
        if value < low or value > high:
            await message.answer(f"⚠️ Нужно число от {low} до {high}. Попробуйте ещё раз.")
            return
        _awaiting.pop(admin_id, None)
        await config.set(key, value)
        await message.answer("Сохранено ✅", reply_markup=_control_keyboard())

    @router.message(Command("forget"))
    async def handle_forget(message: Message, db: Database) -> None:
        """Drop one post from the file_id cache: /forget <ссылка>.

        A post that was delivered once is served from the cache for good, which
        is precisely wrong while its download path is being fixed: the broken
        copy keeps coming back and the fix cannot be seen on the very link that
        showed the problem.
        """
        _, _, argument = (message.text or "").partition(" ")
        url = argument.strip()
        if not url.startswith("http"):
            await message.answer(
                "Пришлите ссылку: <code>/forget https://…</code>" + NEWLINE
                + "Пост скачается заново при следующей отправке."
            )
            return
        if is_short_link(url):
            # The id lives behind the redirect, so a short link hashes to
            # something that cannot be in the cache under any circumstances.
            from bot.downloader import expand_short_link

            url = await expand_short_link(url)
        key = html.escape(canonical_key(url))
        removed = await db.forget_url(hash_url(url))
        if removed:
            await message.answer(
                f"\U0001f5d1 Забыто: <code>{key}</code> — {removed} шт." + NEWLINE
                + "Следующая отправка скачает заново."
            )
        else:
            await message.answer(f"В кэше ничего нет для <code>{key}</code>.")

    def _vpn_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2795 Заменить конфиг",
                                  callback_data=f"{_CONTROL_PREFIX}vpn:replace")],
            [InlineKeyboardButton(text="\u2795 Добавить к текущим",
                                  callback_data=f"{_CONTROL_PREFIX}vpn:append")],
        ])

    def _vpn_text() -> str:
        return (
            "\U0001f6e1 <b>VPN</b>" + NEWLINE + NEWLINE
            + vpnstore.describe() + NEWLINE + NEWLINE
            + "<i>Принимаю: vless/vmess/trojan/ss, hysteria2/tuic, ссылку на "
              "подписку, файл .conf AmneziaWG и готовый config.json xray. "
              "Можно несколько строк сразу.</i>"
        )

    @router.message(Command("vpn"))
    async def handle_vpn(message: Message) -> None:
        await message.answer(_vpn_text(), reply_markup=_vpn_keyboard())

    async def _consume_vpn(message: Message, body: str, filename: str) -> bool:
        """Store a config the admin just sent; True when it was one."""
        payload = vpnstore.classify(body, filename)
        if not payload:
            await message.answer(
                "\u26a0 Не понял, что это за конфиг. Нужны ссылки вида "
                "<code>vless://</code>, <code>hy2://</code>, адрес подписки, "
                "файл AmneziaWG или config.json xray."
            )
            return False
        replace = _awaiting_vpn.get(message.from_user.id, True)
        report = vpnstore.apply_payload(payload, replace=replace)
        _awaiting_vpn.pop(message.from_user.id, None)
        tail = ("" if not payload.unknown
                else NEWLINE + f"<i>Пропущено непонятных строк: {payload.unknown}</i>")
        await message.answer(
            NEWLINE.join(report)
            + NEWLINE + NEWLINE
            + "Применится само в течение минуты — перезапускать ничего не нужно."
            + tail,
            reply_markup=_vpn_keyboard(),
        )
        return True

    @router.message(F.document, lambda m: m.from_user.id in _awaiting_vpn)
    async def handle_vpn_document(message: Message) -> None:
        document = message.document
        if (document.file_size or 0) > 256 * 1024:
            await message.answer("\u26a0 Файл слишком большой для конфига.")
            return
        buffer = await message.bot.download(document)
        body = buffer.read().decode("utf-8", "replace")
        await _consume_vpn(message, body, document.file_name or "")

    @router.message(F.text, lambda m: m.from_user.id in _awaiting_vpn)
    async def handle_vpn_text(message: Message) -> None:
        if (message.text or "").startswith("/"):
            # A command means the admin changed their mind; swallowing it here
            # would make /control and /restart silently do nothing.
            _awaiting_vpn.pop(message.from_user.id, None)
            await message.answer("Отменил ожидание конфига.")
            return
        await _consume_vpn(message, message.text or "", "")

    @router.message(Command("restart"))
    async def handle_admin_restart(message: Message) -> None:
        """Shut down gracefully; Docker's restart policy brings the bot back up.

        Useful to re-read .env and data/cookies.txt, reset in-memory state or
        recover a wedged session — without touching the server. SIGTERM takes
        the same graceful path as `docker stop`.
        """
        logger.info("Admin requested a restart")
        await message.answer(
            "♻️ Перезапускаю бота…\n\n"
            "Бот перечитает конфигурацию и cookies и вернётся через несколько секунд."
        )
        signal.raise_signal(signal.SIGTERM)

    return router
