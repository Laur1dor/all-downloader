"""Terminal status must outlive the progress editor."""

from __future__ import annotations

import asyncio
import unittest
from threading import Event
from unittest.mock import patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from bot.handlers import download
from bot.progress import ProgressState


class TerminalProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_edit_follows_inflight_progress_edit(self) -> None:
        entered = asyncio.Event()
        edits: list[str] = []

        class Message:
            async def edit_text(self, text: str, **_kwargs) -> None:
                if text == "progress":
                    entered.set()
                    await asyncio.sleep(60)
                edits.append(text)

        message = Message()
        with patch.object(download, "EDIT_INTERVAL", 0), \
             patch.object(download, "render_progress", return_value="progress"):
            task = asyncio.create_task(download._edit_progress_loop(
                message, ProgressState(), "аудио", "token", Event(),
            ))
            await asyncio.wait_for(entered.wait(), 1)
            await download._stop_progress(task)
            await download._terminal_edit(message, "⚠️ Ошибка загрузки")

        self.assertEqual(edits, ["⚠️ Ошибка загрузки"])

    async def test_terminal_edit_retries_flood_wait(self) -> None:
        edits: list[str] = []

        class Message:
            async def edit_text(self, text: str, **_kwargs) -> None:
                edits.append(text)
                if len(edits) == 1:
                    raise TelegramRetryAfter(
                        SendMessage(chat_id=1, text=text), "flood", retry_after=0
                    )

        await download._terminal_edit(Message(), "⚠️ Ошибка загрузки")
        self.assertEqual(edits, ["⚠️ Ошибка загрузки"] * 2)

    async def test_terminal_delete_retries_flood_wait(self) -> None:
        attempts = 0

        class Message:
            async def delete(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TelegramRetryAfter(
                        SendMessage(chat_id=1, text="status"), "flood", retry_after=0
                    )

        await download._terminal_delete(Message())
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
