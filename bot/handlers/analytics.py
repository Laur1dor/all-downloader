"""Private operator analytics commands."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from bot.analytics import PERIODS, export_csv, summary
from bot.db import Database


def _period(text: str | None, default: str) -> str:
    parts = (text or "").split(maxsplit=1)
    return parts[1].strip().lower() if len(parts) == 2 else default


def create_router(admin_id: int) -> Router:
    router = Router(name="analytics")
    router.message.filter(F.from_user.id == admin_id, F.chat.type == "private")

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
