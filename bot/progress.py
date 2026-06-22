"""Download/upload progress shared between worker threads and the chat updater.

The yt-dlp hook (worker thread) writes plain numbers into ProgressState; an
asyncio task reads them every couple of seconds and edits the status message.
Cancellation flows the other way: the callback handler sets a threading.Event
that the hook checks on every chunk.

The bar is drawn with coloured blocks (Windows-XP style): real percentages
while downloading, a marquee animation while uploading to Telegram (the Bot
API gives no upload progress callbacks).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# The percent bar appears only for files bigger than this; below it the plain
# status text stays (no message-edit noise for small quick downloads).
PROGRESS_MIN_BYTES = 10 * 1024 * 1024

# Editing a message more often than ~once per 2s runs into Telegram flood limits.
EDIT_INTERVAL = 2.0

_BLOCKS = 10
_FILLED = "🟩"
_EMPTY = "⬜"
_MB = 1024 * 1024

PHASE_DOWNLOAD = "download"
PHASE_UPLOAD = "upload"
# While the bot waits for the user's compress-or-cancel choice the updater
# must not overwrite the question message.
PHASE_PAUSED = "paused"


@dataclass
class ProgressState:
    """Written by the yt-dlp hook (worker thread), read by the updater task."""

    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: int | None = None
    phase: str = PHASE_DOWNLOAD  # set to PHASE_UPLOAD by the handler before sending


class CancelRegistry:
    """Maps active download tokens to their cancellation events."""

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}

    def register(self, token: str) -> threading.Event:
        event = threading.Event()
        self._events[token] = event
        return event

    def cancel(self, token: str) -> bool:
        """Signal cancellation; returns False when the download already finished."""
        event = self._events.get(token)
        if event is None:
            return False
        event.set()
        return True

    def remove(self, token: str) -> None:
        self._events.pop(token, None)


def _bar(filled: int) -> str:
    return _FILLED * filled + _EMPTY * (_BLOCKS - filled)


def render_progress(progress: ProgressState, media_kind: str, tick: int = 0) -> str | None:
    """Build the status message for the current phase; None = keep the previous text."""
    if progress.phase == PHASE_PAUSED:
        return None
    if progress.phase == PHASE_UPLOAD:
        # Marquee: a window of three blocks runs across the bar while uploading.
        offset = tick % _BLOCKS
        blocks = [_EMPTY] * _BLOCKS
        for i in range(3):
            blocks[(offset + i) % _BLOCKS] = _FILLED
        size = f" ({progress.downloaded / _MB:.1f} МБ)" if progress.downloaded else ""
        return f"📤 Отправляю {media_kind}…{size}\n{''.join(blocks)}"

    total = progress.total
    speed = f" • {progress.speed / _MB:.1f} МБ/с" if progress.speed else ""

    if not total:
        # Unknown size (HLS streams): show a moving indicator + downloaded MB so a
        # slow download never looks frozen, even for small clips.
        offset = tick % _BLOCKS
        blocks = [_EMPTY] * _BLOCKS
        for i in range(3):
            blocks[(offset + i) % _BLOCKS] = _FILLED
        return (
            f"⬇️ Скачиваю {media_kind}… {progress.downloaded / _MB:.1f} МБ{speed}\n"
            f"{''.join(blocks)}"
        )

    # Known size: only bother with a bar for sizeable files (avoid edit-spam).
    if total < PROGRESS_MIN_BYTES:
        return None

    percent = min(100, progress.downloaded * 100 // total)
    eta = f" • осталось ~{progress.eta} с" if progress.eta else ""
    return (
        f"⬇️ Скачиваю {media_kind}… {percent}%\n"
        f"{_bar(min(_BLOCKS, percent * _BLOCKS // 100))}\n"
        f"{progress.downloaded / _MB:.1f} / {total / _MB:.1f} МБ{speed}{eta}"
    )
