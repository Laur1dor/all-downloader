"""Private operator analytics commands."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

from bot.analytics import PERIODS, export_csv, summary
from bot.db import Database


async def _open_dashboard(
    message: Message, db: Database, admin_id: int, arguments: str = "",
) -> None:
    from bot.dashboard import DashboardAccess, public_url

    values = arguments.lower().split()
    access = DashboardAccess(db, admin_id)
    if values == ["close"]:
        await access.revoke()
        await message.answer("Доступ к панели закрыт. Все ссылки и сессии отозваны.")
        return
    internet = "local" not in values
    values = [value for value in values if value != "local"]
    periods = {"15": 15, "15m": 15, "30": 30, "30m": 30, "60": 60, "1h": 60,
               "120": 120, "2h": 120}
    if len(values) > 1 or (values and values[0] not in periods):
        await message.answer(
            "/dashboard — панель на 30 минут\n/dashboard 1h — на час\n"
            "/dashboard local — только локальная ссылка\n/dashboard close — закрыть доступ"
        )
        return
    minutes = periods[values[0]] if values else 30
    status = await message.answer("Готовлю защищённую ссылку на статистику…")
    info = await access.issue(minutes, internet=internet)
    remote = None
    if internet:
        for _ in range(40):
            remote = public_url()
            if remote:
                break
            await asyncio.sleep(1)
    base = remote or info["local_url"]
    url = base + "#ticket=" + info["ticket"]
    note = "Доступен из интернета и домашней сети." if remote else "Пока доступен в домашней сети."
    await status.edit_text(
        f"Панель статистики · {minutes} мин\n\n{note}\n"
        "Ссылка для входа одноразовая и действует 5 минут. "
        "После входа сессия сохранится до конца выделенного времени.\n"
        "Для другого устройства запросите новую ссылку. /dashboard close — закрыть доступ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть статистику", url=url),
        ], [InlineKeyboardButton(text="Закрыть доступ", callback_data="dashboardclose")]]),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


def _period(text: str | None, default: str) -> str:
    parts = (text or "").split(maxsplit=1)
    return parts[1].strip().lower() if len(parts) == 2 else default


def create_router(admin_id: int) -> Router:
    router = Router(name="analytics")
    router.message.filter(F.from_user.id == admin_id, F.chat.type == "private")
    router.callback_query.filter(F.from_user.id == admin_id, F.message.chat.type == "private")

    @router.message(Command("dashboard"))
    async def show_dashboard(message: Message, db: Database) -> None:
        parts = (message.text or "").split(maxsplit=1)
        await _open_dashboard(message, db, admin_id, parts[1] if len(parts) > 1 else "")

    @router.callback_query(F.data == "dashboardclose")
    async def close_dashboard(callback: CallbackQuery, db: Database) -> None:
        from bot.dashboard import DashboardAccess

        await callback.answer()
        await DashboardAccess(db, admin_id).revoke()
        if isinstance(callback.message, Message):
            await callback.message.answer("Доступ к панели закрыт. Все сессии отозваны.")

    @router.message(Command("analytics"))
    async def show_analytics(message: Message, db: Database) -> None:
        period = _period(message.text, "24h")
        if period not in PERIODS:
            await message.answer("Период: <code>/analytics 24h</code>, 7d или 30d.")
            return
        await message.answer(await summary(db, period))

    @router.message(Command("analytics_export"))
    async def send_export(message: Message, db: Database) -> None:
        period = _period(message.text, "7d")
        if period not in PERIODS:
            await message.answer("Период: <code>/analytics_export 7d</code> или 30d.")
            return
        content = await export_csv(db, period)
        await message.answer_document(
            BufferedInputFile(content, filename=f"analytics-{period}.csv"),
            caption=(
                "События, результаты загрузок и отправки. "
                "За выбранный период, максимум 5000 последних записей каждого типа. "
                "Тексты сообщений и данные кнопок не включены."
            ),
        )

    return router
