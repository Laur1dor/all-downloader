"""Operator announcement drafts, previews and delivery totals."""

from __future__ import annotations

from aiogram import F, Router, html
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.broadcasts import launch
from bot.db import Database


def create_router(admin_id: int) -> Router:
    router = Router(name="broadcasts")

    @router.message(Command("broadcast"), F.from_user.id == admin_id)
    async def draft(message: Message, db: Database, command: CommandObject) -> None:
        source = message.reply_to_message
        if source is None and command.args:
            source = await message.answer(html.quote(command.args))
        if source is None:
            await message.answer(
                "📣 <b>Рассылки</b>\n\nОтветьте командой /broadcast на готовое сообщение "
                "с текстом, фото или файлом. Форматирование сохранится. "
                "Сначала будет предпросмотр; отправка начнётся после подтверждения.\n\n"
                "Результаты: /broadcasts."
            )
            return
        preview = await message.bot.copy_message(message.chat.id, source.chat.id, source.message_id)
        bid = await db.pool.fetchval(
            "INSERT INTO broadcasts(source_chat,source_message) VALUES($1,$2) RETURNING id",
            message.chat.id,
            preview.message_id,
        )
        total = await db.pool.fetchval("SELECT count(*) FROM users WHERE notifications_enabled")
        await message.answer(
            f"📣 <b>Черновик #{bid}</b>\nПолучателей сейчас: {total}. "
            "Список фиксируется при запуске. Отправка — не быстрее одного сообщения в секунду; "
            "загрузки пользователей имеют приоритет.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Отправить всем", callback_data=f"broadcast:start:{bid}"
                        ),
                        InlineKeyboardButton(
                            text="Отменить", callback_data=f"broadcast:cancel:{bid}"
                        ),
                    ]
                ]
            ),
        )

    @router.callback_query(F.data.startswith("broadcast:"), F.from_user.id == admin_id)
    async def control(call: CallbackQuery, db: Database) -> None:
        _, action, value = call.data.split(":")
        if not value.isdigit():
            await call.answer("Некорректный номер")
            return
        bid = int(value)
        if action == "start":
            changed = await launch(db, bid)
            text = "Рассылка запущена." if changed else "Этот черновик уже обработан."
        elif action == "cancel":
            changed = await db.pool.fetchval(
                "UPDATE broadcasts SET state='cancelled' "
                "WHERE id=$1 AND state IN ('draft','running') "
                "RETURNING id",
                bid,
            )
            text = "Рассылка остановлена." if changed else "Рассылка уже завершена."
        else:
            await call.answer("Неизвестное действие")
            return
        await call.answer(text)
        if call.message:
            await call.message.edit_text(text)

    @router.message(Command("broadcasts"), F.from_user.id == admin_id)
    async def status(message: Message, db: Database) -> None:
        rows = await db.pool.fetch(
            "SELECT b.id,b.state,r.state AS delivery,count(r.telegram_id) AS total FROM "
            "(SELECT * FROM broadcasts ORDER BY id DESC LIMIT 10) b "
            "LEFT JOIN broadcast_recipients r "
            "ON r.broadcast_id=b.id GROUP BY b.id,b.state,r.state ORDER BY b.id DESC"
        )
        states = {
            "draft": "черновик",
            "running": "отправляется",
            "done": "завершена",
            "cancelled": "остановлена",
            "sent": "доставлено",
            "pending": "ожидает",
            "sending": "отправка",
            "uncertain": "доставка не подтверждена",
            "blocked": "блокировка",
            "failed": "ошибка",
            "skipped": "пропущено",
        }
        lines = ["📣 <b>Последние рассылки</b>"]
        for row in rows:
            lines.append(
                f"#{row['id']} · {states.get(row['state'], row['state'])} · "
                f"{states.get(row['delivery'], 'получателей ещё нет')}: {row['total']}"
            )
        running = {r["id"] for r in rows if r["state"] == "running"}
        markup = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"Остановить #{bid}", callback_data=f"broadcast:cancel:{bid}"
                        )
                    ]
                    for bid in sorted(running)
                ]
            )
            if running
            else None
        )
        await message.answer("\n".join(lines), reply_markup=markup)

    return router
