"""Admin commands: statistics and a real bot restart.

The router-level filter rejects everyone except ADMIN_ID on the server side:
for any other user these commands fall through to the regular handlers as if
they did not exist, so they cannot be discovered or bypassed via callbacks.
"""

from __future__ import annotations

import asyncio
import gzip
import html
import logging
import os
import signal
import time
import sys
from contextlib import suppress
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
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


# The reports are the largest thing this bot ever sends to one chat, and they go
# over the same route that drops connections mid-upload. Three things make that
# survivable, none of which were here before: a flat deadline generous enough for
# the payload, retries on ANY network failure, and a size fallback.
#
# Retrying anything is only safe because these land in the operator's own chat: a
# duplicate report is a shrug, where a duplicate video for a user was the bug
# that started all of this. The user-facing sender deliberately refuses to retry
# what may already have arrived; this one deliberately does.
_REPORT_TIMEOUT = 180
_REPORT_ATTEMPTS = 3
# Past this, the markup is shipped compressed. HTML of a table is mostly markup,
# so gzip takes roughly a tenth of it, and a tenth of the bytes is a far bigger
# change in the odds of arriving than any retry policy.
_REPORT_GZIP_OVER = 4 * 1024 * 1024


async def _send_report(message: Message, filename: str, report: str) -> bool:
    """Send one report, returning whether it actually arrived."""
    payload = report.encode("utf-8")
    if len(payload) > _REPORT_GZIP_OVER:
        payload = gzip.compress(payload, compresslevel=6)
        filename += ".gz"
    for attempt in range(1, _REPORT_ATTEMPTS + 1):
        try:
            await message.bot(
                message.answer_document(
                    BufferedInputFile(payload, filename=filename)
                ),
                request_timeout=_REPORT_TIMEOUT,
            )
            return True
        except TelegramNetworkError as error:
            logger.warning(
                "Report %s attempt %d/%d failed (%s)",
                filename, attempt, _REPORT_ATTEMPTS, error.__cause__ or error,
            )
            if attempt == _REPORT_ATTEMPTS:
                return False
            await asyncio.sleep(2.0 * attempt)
    return False


_PERIOD_PREFIX = "stat:"
# Hours, and what to call them. None is the whole history.
_PERIODS: tuple[tuple[str, str, int | None], ...] = (
    ("24h", "24 часа", 24),
    ("7d", "7 дней", 24 * 7),
    ("30d", "30 дней", 24 * 30),
    ("all", "всё время", None),
)


def _gb(value) -> str:
    gigabytes = (value or 0) / (1024 ** 3)
    return f"{gigabytes:.2f} ГБ" if gigabytes >= 0.01 else f"{(value or 0) / 1048576:.0f} МБ"


def _secs(value) -> str:
    return "—" if value is None else f"{float(value):.1f} с"


def _period_keyboard(active: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=(f"• {label} •" if key == active else label),
            callback_data=f"{_PERIOD_PREFIX}{key}",
        )
        for key, label, _ in _PERIODS
    ]])


def _render_stats(label: str, data: dict) -> str:
    totals = data["totals"]
    uploads = data["uploads"]
    total = totals["total"] or 0
    done = totals["done"] or 0
    share = f"{done * 100 // total}%" if total else "—"
    lines = [
        f"\U0001f4c8 <b>Итоги — {label}</b>",
        "",
        f"\U0001f3ac Запросов: <b>{total}</b> "
        f"(\u2705 {done} / \u274c {totals['failed'] or 0} / "
        f"\U0001f6ab {totals['cancelled'] or 0}) — успех <b>{share}</b>",
        f"\U0001f465 Активных: <b>{totals['users'] or 0}</b>, "
        f"новых: <b>{data['new_users'] or 0}</b>",
        f"\U0001f4be Отдано: <b>{_gb(totals['bytes'])}</b>",
        f"\u23f1 Время запроса: медиана <b>{_secs(totals['median_seconds'])}</b>, "
        f"95-й процентиль <b>{_secs(totals['p95_seconds'])}</b>",
    ]

    if uploads and (uploads["total"] or 0):
        speed = uploads["median_bps"]
        speed_text = f"{float(speed) / 1048576:.1f} МБ/с" if speed else "—"
        lines += [
            "",
            "\U0001f4e4 <b>Отправка</b> — это и есть ответ про канал:",
            f"   скорость (медиана): <b>{speed_text}</b>",
            f"   ждали очереди: <b>{uploads['queued'] or 0}</b> из "
            f"{uploads['total']}, 95-й процентиль "
            f"<b>{_secs(uploads['p95_wait'])}</b>",
            f"   максимум одновременно рядом: <b>{uploads['max_alongside'] or 0}</b>",
        ]
    else:
        lines += ["", "<i>Замеров отправки за период пока нет.</i>"]

    platforms = data["platforms"]
    if platforms:
        lines += ["", "<b>По площадкам</b>"]
        for row in platforms:
            row_total = row["total"] or 0
            row_done = row["done"] or 0
            ok = f"{row_done * 100 // row_total}%" if row_total else "—"
            lines.append(
                f"   {row['platform']}: {row_total} ({ok}), "
                f"{_gb(row['bytes'])}, медиана {_secs(row['median_seconds'])}"
            )

    busiest = data["busiest"]
    if busiest:
        hours = ", ".join(
            f"{int(r['hour']):02d}:00 ({r['total']})" for r in busiest
        )
        lines += ["", f"\U0001f552 Пик (UTC): {hours}"]
    return NEWLINE.join(lines)


# The login needs a browser, which lives in its own container; the bot asks for
# one by dropping a file, the same way the tunnels are asked to reload. Handing
# the bot the docker socket would hand it root on the host, and a login helper
# is not worth that.
_IGLOGIN_REQUEST = Path(os.getenv("IG_REQUEST_FILE", "data/iglogin_request"))
_IGLOGIN_RESULT = Path(os.getenv("IG_RESULT_FILE", "data/iglogin_result.txt"))
# Long, because the slow part is waiting for Instagram to send a code by mail.
_IGLOGIN_TIMEOUT = 420


async def _run_iglogin() -> tuple[bool, str]:
    """Ask the browser container to sign in; returns (ok, what it said)."""
    with suppress(FileNotFoundError):
        _IGLOGIN_RESULT.unlink()
    _IGLOGIN_REQUEST.parent.mkdir(parents=True, exist_ok=True)
    _IGLOGIN_REQUEST.write_text(str(int(time.time())), encoding="utf-8")

    deadline = time.monotonic() + _IGLOGIN_TIMEOUT
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        if not _IGLOGIN_RESULT.exists():
            continue
        report = _IGLOGIN_RESULT.read_text(encoding="utf-8", errors="replace")
        head, _, body = report.partition(NEWLINE)
        return head.strip() == "OK", body.strip() or head.strip()
    return False, (
        "Не дождался ответа. Контейнер iglogin запущен? "
        "docker compose up -d iglogin"
    )


# A CAPTCHA on the account is for a person. This puts the server's own browser
# in front of one - see scripts/igremote.py - because the session that has to
# answer it lives in that browser's profile, not on anybody's phone.
_IGREMOTE_REQUEST = Path(os.getenv("IG_REMOTE_REQUEST", "data/igremote_request"))
_IGREMOTE_STATUS = Path(os.getenv("IG_REMOTE_STATUS", "data/igremote_status.json"))
_IGREMOTE_URL = os.getenv("IG_REMOTE_URL", "").rstrip("/")
_IGREMOTE_LIFETIME = int(os.getenv("IG_REMOTE_LIFETIME", "1200"))


def _igremote_status() -> dict:
    try:
        import json as _json

        return _json.loads(_IGREMOTE_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
        sent, failed = [], []
        for name, build in (
            ("users.html", db.export_users_html),
            ("conversions.html", db.export_conversions_html),
        ):
            try:
                report = await build()
            except Exception:
                logger.exception("Could not build %s", name)
                failed.append(name)
                continue
            if await _send_report(message, name, report):
                sent.append(name)
            else:
                failed.append(name)
        if failed:
            # Saying which file did not make it is the difference between "the
            # command is broken" and "the link dropped one file, ask again".
            await message.answer(
                "⚠️ Не удалось отправить: <b>" + ", ".join(failed) + "</b>."
                + (" Отправлено: " + ", ".join(sent) + "." if sent else "")
                + " Попробуйте ещё раз."
            )

    @router.message(Command("stats"))
    async def handle_stats(message: Message, db: Database) -> None:
        data = await db.fetch_period_stats(24)
        await message.answer(
            _render_stats("24 часа", data), reply_markup=_period_keyboard("24h")
        )

    @router.callback_query(F.data.startswith(_PERIOD_PREFIX))
    async def handle_stats_period(callback: CallbackQuery, db: Database) -> None:
        key = callback.data.removeprefix(_PERIOD_PREFIX)
        chosen = next((p for p in _PERIODS if p[0] == key), None)
        if chosen is None:
            await callback.answer()
            return
        _, label, hours = chosen
        data = await db.fetch_period_stats(hours)
        await callback.answer()
        if isinstance(callback.message, Message):
            with suppress(TelegramBadRequest):
                await callback.message.edit_text(
                    _render_stats(label, data), reply_markup=_period_keyboard(key)
                )

    @router.message(Command("iglogin"))
    async def handle_iglogin(message: Message, ig_session=None) -> None:
        """Log into Instagram from the server and store the session.

        The manual loop failed three times running: export cookies from a
        browser, watch them die a fortnight later, export them again. This does
        the same thing from the machine that actually uses the session, so it
        can be repeated without anybody being there.
        """
        if not os.getenv("IG_USERNAME"):
            await message.answer(
                "\u26a0 Не настроено. В <code>.env</code> нужны "
                "<code>IG_USERNAME</code>, <code>IG_PASSWORD</code> и доступ к "
                "почте: <code>IG_IMAP_HOST</code>, <code>IG_IMAP_USER</code>, "
                "<code>IG_IMAP_PASSWORD</code>."
            )
            return
        notice = await message.answer(
            "\U0001f511 Вхожу в Instagram… это может занять пару минут: "
            "надо дождаться кода на почту."
        )
        # Through the watch when there is one: it holds the only lock over
        # the request/result files, and two callers racing on those is how a
        # login that worked gets reported as a timeout.
        if ig_session is not None and getattr(ig_session, "enabled", False):
            ok, report = await ig_session.login_now()
        else:
            ok, report = await _run_iglogin()
        head = "\u2705 Сессия получена." if ok else "\u26a0 Войти не удалось."
        with suppress(TelegramBadRequest):
            await notice.edit_text(
                head + NEWLINE + NEWLINE + "<pre>" + html.escape(report) + "</pre>"
            )

    async def _igcaptcha_flow(target: Message, ig_session) -> None:
        """Open the server's browser for a person and hand them the link.

        Reached from /igcaptcha and from the button under the CAPTCHA notice,
        so the link always arrives in this chat rather than anywhere else.
        """
        with suppress(FileNotFoundError):
            _IGREMOTE_STATUS.unlink()
        _IGREMOTE_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        _IGREMOTE_REQUEST.write_text(str(int(time.time())), encoding="utf-8")
        notice = await target.answer("\U0001f5a5 Поднимаю браузер сервера…")

        status: dict = {}
        for _ in range(60):
            await asyncio.sleep(1.5)
            status = _igremote_status()
            if status.get("state") in ("open", "failed", "signed_in"):
                break
        if status.get("state") == "signed_in":
            await notice.edit_text("\u2705 Капчи нет, сессия в порядке.")
            return
        if status.get("state") != "open":
            await notice.edit_text(
                "\u26a0 Браузер не поднялся: "
                + html.escape(str(status.get("error") or "нет ответа"))
            )
            return

        query = f"autoconnect=1&resize=scale&password={status['password']}"
        path = f"/{status['token']}/vnc.html?{query}"
        links = []
        if status.get("public"):
            links.append(f'<a href="{html.escape(status["public"] + path)}">'
                         "Открыть браузер сервера</a>")
        if _IGREMOTE_URL:
            links.append(f'<a href="{html.escape(_IGREMOTE_URL + path)}">'
                         "Из домашней сети</a>")
        if not links:
            await notice.edit_text("\u26a0 Нет ни публичного адреса, ни "
                                   "<code>IG_REMOTE_URL</code> — ссылку дать некуда.")
            return

        lead = ("Instagram показывает капчу." if status.get("challenged")
                else "Капчи сейчас нет — можно просто проверить, что всё в порядке.")
        await notice.edit_text(
            f"\U0001f5a5 {lead}" + NEWLINE + NEWLINE
            + NEWLINE.join(links) + NEWLINE + NEWLINE
            + "Пройди капчу сам — как только Instagram её уберёт, бот сохранит "
            "сессию и закроет доступ. Ссылка одноразовая и живёт "
            f"{_IGREMOTE_LIFETIME // 60} минут.",
            disable_web_page_preview=True,
        )

        deadline = time.monotonic() + _IGREMOTE_LIFETIME + 60
        state = "expired"
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            state = _igremote_status().get("state") or state
            if state in ("signed_in", "cleared_not_signed_in", "expired", "failed"):
                break

        from bot import igbrowser

        if state == "signed_in":
            igbrowser._captcha_until = 0.0
            await target.answer("\u2705 Капча пройдена, сессия сохранена. "
                                "Посты 18+ снова качаются.")
        elif state == "cleared_not_signed_in":
            igbrowser._captcha_until = 0.0
            await target.answer("\u2705 Капча пройдена. Вхожу в аккаунт…")
            if ig_session is not None and getattr(ig_session, "enabled", False):
                ok, report = await ig_session.login_now()
                if ok:
                    await target.answer("\u2705 Вошёл.")
                else:
                    await target.answer("\u26a0 Войти не вышло: <pre>"
                                        + html.escape(report[-600:]) + "</pre>")
        elif state == "expired":
            await target.answer("\u231b Время вышло, браузер закрыт. "
                                "Можно запустить /igcaptcha ещё раз.")
        else:
            await target.answer("\u26a0 Сессия с браузером оборвалась.")

    @router.message(Command("igcaptcha"))
    async def handle_igcaptcha(message: Message, ig_session=None) -> None:
        await _igcaptcha_flow(message, ig_session)

    @router.callback_query(F.data == "igcaptcha")
    async def handle_igcaptcha_button(callback: CallbackQuery, ig_session=None) -> None:
        await callback.answer()
        if callback.message is not None:
            with suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(reply_markup=None)
            await _igcaptcha_flow(callback.message, ig_session)

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
