"""Video download by link, audio extraction and download cancellation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext, suppress
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, F, Router, html
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from bot.config import Settings
from bot.db import STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, Database, hash_url
from bot.downloader import (
    PHOTO_CAPABLE_PLATFORMS,
    ConfirmedVideoError,
    DownloadCancelledError,
    DownloadFailedError,
    FileTooLargeError,
    NotAVideoPostError,
    OversizedError,
    QualityOption,
    detect_platform,
    download_album,
    download_audio,
    download_video,
    expand_short_link,
    fetch_description,
    is_audio_only_platform,
    is_music_album,
    is_photo_post_url,
    is_youtube_music,
    is_youtube_shorts,
    photo_needs_document,
    probe_quality_options,
    quality_selector,
    request_tiktok_reprobe,
    resolve_download_url,
    video_metadata,
)
from bot.handlers.events import mark_outcome
from bot.media_parts import (
    MAX_PARTS,
    MAX_SOURCE_BYTES,
    MediaPreparationError,
    parse_time_range,
    prepare_audio,
    prepare_streaming_mp4,
    prepare_video,
)
from bot.progress import (
    EDIT_INTERVAL,
    PHASE_DOWNLOAD,
    PHASE_PAUSED,
    PHASE_PREPARE,
    PHASE_QUEUE,
    PHASE_UPLOAD,
    CancelRegistry,
    ProgressState,
    render_progress,
)
from bot.proxy import forced_proxy, proxy_for
from bot.runtime import config
from bot.telegram_outbound import progress_call
from bot.urlcache import UrlCache
from bot.urlguard import BlockedAddressError, ensure_public_url
from bot.yandex_preview import (
    PreviewUnavailableError,
    YandexPreview,
    resolve_yandex_preview,
)

logger = logging.getLogger(__name__)

router = Router(name="download")

_AUDIO_CALLBACK_PREFIX = "audio:"
_CANCEL_CALLBACK_PREFIX = "cancel:"
_COMPRESS_YES_PREFIX = "cmpy:"
_COMPRESS_NO_PREFIX = "cmpn:"
_YT_VIDEO_PREFIX = "ytv:"
_YT_AUDIO_PREFIX = "yta:"
_QUALITY_PREFIX = "qh:"
_DESCRIPTION_PREFIX = "desc:"
_TIKTOK_FILE_PREFIX = "ttfile:"
_TIKTOK_DOCUMENT_CACHE_KEY = "tiktok_document_source_v2"
# How long interactive questions (compress? video-or-audio?) stay active.
_COMPRESS_CHOICE_TIMEOUT = 10.0
_MEDIA_CHOICE_TIMEOUT = 10.0
# Upload timeout for large files; the local Bot API hop is local so this is generous.
# Uploads are bounded by the file's own size rather than by one blanket number.
# Measured on 16 Sep 2026: the far side of this route hangs up after almost
# exactly 500s, three separate times, so any budget above that is never reached —
# the upload dies on someone else's timer and we learn nothing from it. Staying
# under that line keeps the deadline ours: it ends when we say so, with a message
# we choose. The slope is deliberately generous — a megabyte every six seconds
# against the 10-12 MB/s this path actually measures — so a healthy upload never
# trips it, while a 0.5 MB TikTok clip stops waiting after a minute instead of
# eight.
_UPLOAD_KILL_SECONDS = 500
_UPLOAD_FLOOR_SECONDS = 60
_UPLOAD_SECONDS_PER_MB = 6
# At 20 Mbit/s, 768 MiB takes ~322s, leaving room before the 480s
# application deadline and the route's 500s kill.  The encoder targets 75%
# of this limit, so a six-GiB source still fits at most eight parts.
_UPLOAD_PART_BYTES = 768 * 1024**2


def _upload_timeout(size_bytes: int | None) -> int:
    """Seconds to allow for one upload of this size."""
    megabytes = (size_bytes or 0) / (1024 * 1024)
    budget = _UPLOAD_FLOOR_SECONDS + megabytes * _UPLOAD_SECONDS_PER_MB
    return int(min(budget, _UPLOAD_KILL_SECONDS - 20))
# Telegram rejects photos larger than 10 MB — bigger images are sent as files.
_PHOTO_MAX_BYTES = 10 * 1024 * 1024

# token -> (event, state) for pending compress-or-cancel questions.
_pending_choices: dict[str, tuple[asyncio.Event, dict]] = {}

# token -> post caption, for album description buttons (albums carry no inline
# keyboard, and a photo post's caption can't be re-fetched via yt-dlp).
_album_descriptions: OrderedDict[str, str] = OrderedDict()

# Telegram file_ids preserve a video as a video, so sendDocument cannot reuse
# one. Keep a short-lived hard link to the already validated TikTok MP4 for the
# file button; this avoids a second fetch from a rate-limited TikTok endpoint.
_TIKTOK_ORIGINAL_TTL = 3600
_TIKTOK_ORIGINAL_MAX_BYTES = 256 * 1024 * 1024
_tiktok_original_dir: tempfile.TemporaryDirectory | None = None
_tiktok_originals: OrderedDict[str, tuple[Path, int, float]] = OrderedDict()


def _prune_tiktok_originals(now: float, needed: int = 0) -> None:
    total = sum(size for _, size, _ in _tiktok_originals.values())
    for key, (path, size, expires) in list(_tiktok_originals.items()):
        if expires <= now or total + needed > _TIKTOK_ORIGINAL_MAX_BYTES:
            _tiktok_originals.pop(key)
            path.unlink(missing_ok=True)
            total -= size


def _remember_tiktok_original(url: str, source: Path) -> None:
    """Retain original bytes with a hard link; never duplicate a large upload."""
    global _tiktok_original_dir
    size = source.stat().st_size
    if size > _TIKTOK_ORIGINAL_MAX_BYTES:
        return
    now = time.monotonic()
    _prune_tiktok_originals(now, size)
    key = hash_url(url)
    existing = _tiktok_originals.pop(key, None)
    if existing is not None:
        existing[0].unlink(missing_ok=True)
    if _tiktok_original_dir is None:
        _tiktok_original_dir = tempfile.TemporaryDirectory(prefix="tg-tiktok-originals-")
    destination = Path(_tiktok_original_dir.name) / f"{key}.mp4"
    os.link(source, destination)
    _tiktok_originals[key] = (destination, size, now + _TIKTOK_ORIGINAL_TTL)


def _tiktok_original_path(url: str) -> Path | None:
    _prune_tiktok_originals(time.monotonic())
    entry = _tiktok_originals.get(hash_url(url))
    return entry[0] if entry and entry[0].is_file() else None


def _remember_description(token: str, description: str) -> None:
    _album_descriptions[token] = description
    while len(_album_descriptions) > 500:
        _album_descriptions.popitem(last=False)

# Machine protection: cap concurrent non-admin downloads. The admin bypasses it.
_download_slots = asyncio.Semaphore(3)
# ffmpeg can use multiple CPU threads and an extra copy of the source. Keep
# expensive trims/splits serial; a short file that needs no work bypasses it.
_prepare_slots = asyncio.Semaphore(1)
_large_source_slots = asyncio.Semaphore(1)
_large_source_waiters = 0
_MAX_LARGE_SOURCE_WAITERS = 2
_LARGE_SOURCE_WAIT_SECONDS = 10 * 60


@asynccontextmanager
async def _admitted_media(
    context, is_admin: bool, *, large_source: bool = False,
    large_if_bytes: int | None = None,
    progress: ProgressState | None = None, cancel_event: Event | None = None,
):
    """Bound retained large sources; release network slots before preparation."""
    global _large_source_waiters
    acquired_large = False
    if large_source:
        if _large_source_slots.locked() and _large_source_waiters >= _MAX_LARGE_SOURCE_WAITERS:
            raise DownloadFailedError("Очередь больших файлов заполнена. Попробуйте позже.")
        _large_source_waiters += 1
        if progress is not None:
            progress.phase = PHASE_QUEUE
            progress.started_at = time.monotonic()
        try:
            deadline = time.monotonic() + _LARGE_SOURCE_WAIT_SECONDS
            while not acquired_large:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError
                if time.monotonic() >= deadline:
                    raise DownloadFailedError("Слишком долго ожидаю очередь больших файлов.")
                try:
                    await asyncio.wait_for(_large_source_slots.acquire(), timeout=1)
                    acquired_large = True
                except TimeoutError:
                    continue
        finally:
            _large_source_waiters -= 1
        if progress is not None:
            progress.phase = PHASE_DOWNLOAD
            progress.started_at = time.monotonic()
    try:
        async with AsyncExitStack() as stack:
            async with (nullcontext() if is_admin else _download_slots):
                media = await stack.enter_async_context(context)
                if (
                    not acquired_large and large_if_bytes is not None
                    and (getattr(media, "file_size", 0) or 0) > large_if_bytes
                ):
                    # A short-form post can unexpectedly be enormous.  Do not
                    # retain it in an unbounded ffmpeg queue or block the
                    # download lane while another large job owns that queue.
                    if _large_source_slots.locked():
                        raise DownloadFailedError(
                            "Очередь больших файлов заполнена. Попробуйте позже."
                        )
                    await _large_source_slots.acquire()
                    acquired_large = True
            yield media
    finally:
        if acquired_large:
            _large_source_slots.release()

# Upload admission. Uploads used to be serialised outright, one at a time, and a
# single wedged one held that place for its whole retry ladder — which is how one
# stuck 0.5 MB clip made everybody else wait. Measured offered load is about
# 0.3% of the link (roughly fifty uploads a day of a few seconds each), so there
# is no congestion here to regulate and nothing a regulator could even measure at
# two samples an hour. The problem was never throughput; it was one job blocking
# the queue. So: a second lane, and a lane of its own for large files, which
# would otherwise halve each other's speed and double the time both spend
# exposed to a route that drops connections.
#
# Raised from two small places to three, and the boundary from 32 to 64 MB. 64
# is where the per-file deadline still has room: it budgets 60s + 6s/MB and caps
# at 480s, so a 64 MB file gets 444s of its own while anything past ~70 MB is on
# the cap and has stopped scaling — which is the honest line between "a clip"
# and "a big file", rather than a round number.
_UPLOAD_LANE_BYTES = 64 * 1024 * 1024
_UPLOAD_LANE_SMALL = 3
_UPLOAD_LANE_LARGE = 1
# The lanes are separate, so without this a large upload and a full small lane
# would add up to one more concurrent upload than either number suggests. This
# is the number that actually meets the link.
_UPLOAD_TOTAL = 3


class _UploadCapacity:
    """Who may upload right now.

    The admin never waits — that is the whole of "admin first" that can honestly
    be offered, since an HTTP upload already in flight cannot be cut short. On
    top of admission, while an admin upload is running the users' small lane
    gives up one of its places, so the operator gets a larger share of the link
    rather than merely being let in first.

    Deliberately not an asyncio.Semaphore that gets resized: shrinking one means
    absorbing permits, and two shrinks racing with releases can strand them for
    good, leaving a limiter that admits nobody and can never recover.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._small = 0
        self._large = 0
        self._admin = 0

    def _has_room(self, large: bool) -> bool:
        # max(1, ...) — the reservation narrows a lane, it never closes it, so
        # there is no state in which a user upload can never be admitted.
        if self._small + self._large >= max(1, _UPLOAD_TOTAL - self._admin):
            return False
        if large:
            return self._large < _UPLOAD_LANE_LARGE
        return self._small < max(1, _UPLOAD_LANE_SMALL - self._admin)

    @asynccontextmanager
    async def slot(
        self,
        size_bytes: int | None,
        is_admin: bool,
        db: Database | None = None,
        platform: str | None = None,
    ):
        large = (size_bytes or 0) >= _UPLOAD_LANE_BYTES
        queued_at = time.monotonic()
        async with self._cond:
            if is_admin:
                self._admin += 1
            else:
                await self._cond.wait_for(lambda: self._has_room(large))
                if large:
                    self._large += 1
                else:
                    self._small += 1
            self._cond.notify_all()
            started_at = time.monotonic()
            # Logged so the question "how many of these can the link carry at
            # once" gets answered by the traffic that actually runs here, rather
            # than by a load test against the machine people are using.
            alongside = self._small + self._large + self._admin - 1
        try:
            yield
        finally:
            async with self._cond:
                if is_admin:
                    self._admin -= 1
                elif large:
                    self._large -= 1
                else:
                    self._small -= 1
                # notify_all, not notify: a release can free room for a waiter in
                # either lane, and waking only one can leave the other asleep
                # until a release that may never come.
                self._cond.notify_all()
            elapsed = time.monotonic() - started_at
            waited = started_at - queued_at
            megabytes = (size_bytes or 0) / (1024 * 1024)
            logger.info(
                "upload done: %.2f MB in %.1fs (%.2f MB/s), waited %.1fs, "
                "alongside %d, lane=%s%s",
                megabytes, elapsed,
                megabytes / elapsed if elapsed > 0.05 else 0.0,
                waited, alongside,
                "large" if large else "small",
                ", admin" if is_admin else "",
            )
            if db is not None:
                # Kept out of the lock above, and never allowed to fail the
                # upload it is describing.
                await db.record_upload(
                    platform, "large" if large else "small", is_admin,
                    size_bytes, elapsed, waited, alongside,
                )


_upload_capacity = _UploadCapacity()


def _remove_completed_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not release completed media file %s", path, exc_info=True)


def _release_source_after_split(source: Path, upload_paths: list[Path]) -> None:
    """A verified multipart output no longer needs its full source on disk."""
    if (
        len(upload_paths) > 1
        and source not in upload_paths
        and all(path.is_file() and path.stat().st_size > 0 for path in upload_paths)
    ):
        _remove_completed_file(source)


async def _send_prepared_part(
    build: Callable[[], Any], bot: Bot, timeout: int,
    path: Path, multipart: bool,
) -> Message:
    sent = await _send_with_retries(build, bot, timeout)
    if multipart:
        _remove_completed_file(path)
    return sent


async def _send_paced(
    build: Callable[[], Awaitable[Any] | TelegramMethod[Any]], timeout: int,
) -> Any:
    """Keep the media upload deadline; the session admits and retries calls."""
    request = build()
    if isinstance(request, TelegramMethod) and request.bot is not None:
        # Message.answer_* binds a method, but awaiting it uses the session's
        # 90-second default. Pass the per-file deadline into aiogram itself.
        return await asyncio.wait_for(
            request.bot(request, request_timeout=timeout), timeout=timeout,
        )
    return await asyncio.wait_for(request, timeout=timeout)

# Anti-spam: each non-admin user may have only one link download in flight.
# A user who pastes more links while busy is warned once and then ignored.
_busy_users: set[int] = set()
_warned_users: set[int] = set()

# A link that sat in Telegram's queue while the bot was wedged is almost never
# still wanted by the time it is delivered: the person gave up, or sent it again,
# and downloading all of it at once is how the bot came back from a freeze and
# immediately buried itself.
#
# This is the age of the MESSAGE when the bot picks it up, checked once, before
# any work starts — not how long a download may take. A file that needs forty
# minutes is unaffected; the check is long behind it by then.
_STALE_AFTER_SECONDS = int(os.getenv("STALE_LINK_SECONDS", "180"))

# Per-user token bucket: three downloads a minute, refilling one every twenty
# seconds. Charged where real work begins, so a stale link, a cache hit or the
# audio branch cost nothing — they cost the machine nothing either. Pasting one
# link repeatedly is held by the flood limit in bot/handlers/flood.py, which
# counts messages; this one counts work.
_RATE_TOKENS = 3.0
_RATE_WINDOW_SECONDS = 60.0
_rate_buckets: dict[int, tuple[float, float]] = {}

# Expansion is a walk over the proxy ladder, so ten identical pastes would pay it
# ten times before anything could notice they are the same link. Remembering the
# raw string is what makes a repeated paste cheap.
_expanded_links: OrderedDict[str, str] = OrderedDict()


def _remember_expansion(raw: str, expanded: str) -> None:
    _expanded_links[raw] = expanded
    _expanded_links.move_to_end(raw)
    while len(_expanded_links) > 500:
        _expanded_links.popitem(last=False)


def _rate_delay(user_id: int) -> float:
    """Seconds this user must wait, or 0.0 when they may proceed.

    A token is taken on success, so this both asks and charges.
    """
    now = time.monotonic()
    tokens, stamp = _rate_buckets.get(user_id, (_RATE_TOKENS, now))
    tokens = min(
        _RATE_TOKENS,
        tokens + (now - stamp) * _RATE_TOKENS / _RATE_WINDOW_SECONDS,
    )
    if tokens < 1.0:
        _rate_buckets[user_id] = (tokens, now)
        return (1.0 - tokens) * _RATE_WINDOW_SECONDS / _RATE_TOKENS
    _rate_buckets[user_id] = (tokens - 1.0, now)
    return 0.0


@asynccontextmanager
async def _user_gate(user_id: int, is_admin: bool, message: Message):
    """Allow one in-flight download per non-admin user; the admin is unrestricted."""
    if is_admin:
        yield True
        return
    if user_id in _busy_users:
        if user_id not in _warned_users:
            _warned_users.add(user_id)
            await message.answer("⚠️ Дождитесь окончания вашей текущей загрузки.")
        yield False
        return
    _busy_users.add(user_id)
    try:
        yield True
    finally:
        _busy_users.discard(user_id)
        _warned_users.discard(user_id)


def _user_limit(settings: Settings, user_id: int) -> int:
    """Per-audience upload limit (admin-tunable), capped by the server's hard max."""
    is_admin = user_id == settings.admin_id
    return min(config.upload_bytes(is_admin), settings.max_upload_bytes)


def _part_upload_limit(settings: Settings, user_id: int) -> int:
    """Keep one network upload below the route deadline at home uplink rates."""
    configured_mbit = config.get("bandwidth_mbit")
    effective_mbit = min(20, configured_mbit) if configured_mbit > 0 else 20
    return min(
        _user_limit(settings, user_id),
        max(10 * 1024**2, _UPLOAD_PART_BYTES * effective_mbit // 20),
    )


def _download_limit(settings: Settings, user_id: int) -> int:
    """Bound aggregate bytes even when each Telegram upload fits separately."""
    free = shutil.disk_usage(tempfile.gettempdir()).free
    return min(MAX_SOURCE_BYTES, _part_upload_limit(settings, user_id) * MAX_PARTS,
               max(0, (free - 512 * 1024**2) // 2))


def _needs_large_video_slot(
    offers_quality: bool, time_range: tuple[int, int] | None,
    estimated_size: int | None, part_limit: int,
) -> bool:
    """A known small quality must bypass another user's large source job."""
    return time_range is not None or (
        offers_quality and (estimated_size is None or estimated_size > part_limit)
    )


def _ratelimit_bps(
    user_id: int, settings: Settings, platform: str, force_proxy: bool
) -> int | None:
    """yt-dlp download rate cap (bytes/s): per-audience, tightened on VLESS routes."""
    is_admin = user_id == settings.admin_id
    via_proxy = force_proxy or bool(proxy_for(platform))
    return config.ratelimit_bps(is_admin, via_proxy)

_CAPTION = (
    "Скачано с помощью:\n"
    '<a href="https://t.me/TikTokDownloaderFF_bot">@TikTokDownloaderFF_bot</a>'
)


def _description_capable(url: str, platform: str) -> bool:
    """Platforms whose posts carry a text description worth a button."""
    if platform in ("tiktok", "instagram", "twitter"):
        return True
    return platform == "youtube" and is_youtube_shorts(url)


def _video_keyboard(
    token: str, with_description: bool = False, tiktok_file: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons under a delivered video: audio always, description when present."""
    buttons = [
        InlineKeyboardButton(
            text="🎵 Музыка", callback_data=f"{_AUDIO_CALLBACK_PREFIX}{token}"
        )
    ]
    if with_description:
        buttons.append(
            InlineKeyboardButton(
                text="📝 Описание", callback_data=f"{_DESCRIPTION_PREFIX}{token}"
            )
        )
    if tiktok_file:
        buttons.append(InlineKeyboardButton(
            text="📁 Файл", callback_data=f"{_TIKTOK_FILE_PREFIX}{token}"
        ))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _description_keyboard(token: str, tiktok_file: bool = False) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(
        text="📝 Описание", callback_data=f"{_DESCRIPTION_PREFIX}{token}",
    )]
    if tiktok_file:
        buttons.append(InlineKeyboardButton(
            text="📁 Файл", callback_data=f"{_TIKTOK_FILE_PREFIX}{token}"
        ))
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons]
    )


def _cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"{_CANCEL_CALLBACK_PREFIX}{token}",
                )
            ]
        ]
    )


async def _delete_silently(message: Message) -> None:
    with suppress(TelegramBadRequest):
        await message.delete()


def _never_reached_telegram(error: TelegramNetworkError) -> bool:
    """Whether the request provably never got to the server.

    Only a failure to open the connection proves it: the body was never sent, so
    sending it again cannot put a second copy in the chat. Everything else — a
    timeout, or the server hanging up while the response is awaited — leaves the
    upload's fate unknown, and unknown has to be read as delivered.

    This is not hypothetical. The failure measured on 16 Sep was
    ServerDisconnectedError raised from resp.start(): after the whole body had
    gone out, while the response was awaited. A 0.75 MB file is on this wire in
    under a second, so Telegram had it. Re-sending put the same video in the chat
    again, and again — which is what people described as the bot spamming them.
    """
    return isinstance(error.__cause__, aiohttp.ClientConnectorError)


async def _send_with_retries(
    build: Callable[[], Any],
    bot: Bot,
    timeout: int,
    attempts: int = 3,
    delay: float = 2.0,
) -> Message:
    """Send media under its own deadline, retrying only what delivered nothing.

    The deadline covers the answer_* coroutine, including Telegram's upload.
    A fresh coroutine is built for each safe retry.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await _send_paced(build, timeout)
        except TelegramNetworkError as error:
            if attempt == attempts or not _never_reached_telegram(error):
                raise
            # The cause is logged now: "attempt N failed" said nothing about
            # which failure it was, and that is the one thing that decides
            # whether a retry is safe.
            logger.warning(
                "Upload attempt %d/%d could not connect (%s), retrying",
                attempt, attempts, error.__cause__ or error,
            )
            await asyncio.sleep(delay * attempt)
    raise AssertionError("unreachable")


async def _edit_progress_loop(
    status_message: Message,
    progress: ProgressState,
    media_kind: str,
    token: str,
    cancel_event: Event,
) -> None:
    """Redraw the status message through download and upload until cancelled.

    Stops touching the message once the user pressed cancel, so the
    "Отменяю…" feedback from the callback handler stays visible.
    """
    last_text = None
    tick = 0
    while not cancel_event.is_set():
        await asyncio.sleep(EDIT_INTERVAL)
        tick += 1
        if cancel_event.is_set():
            return
        text = render_progress(progress, media_kind, tick)
        if text is None or text == last_text:
            continue
        last_text = text
        markup = None if progress.phase == PHASE_UPLOAD else _cancel_keyboard(token)
        with suppress(TelegramBadRequest, TelegramRetryAfter), progress_call():
            await status_message.edit_text(text, reply_markup=markup)


async def _stop_progress(task: asyncio.Task) -> None:
    """Wait for any in-flight progress edit before changing terminal status."""
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class _AlbumStatus:
    """Only report elapsed time and items Telegram has actually accepted."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.phase = "download"
        self.sent = 0
        self.total = 0

    def render(self) -> str:
        elapsed = int(time.monotonic() - self.started_at)
        if self.phase == "download":
            return f"📸 Получаю медиа поста… {elapsed} с"
        if self.phase == "prepare":
            return f"🧩 Проверяю медиа поста… {elapsed} с"
        return f"📤 Отправляю пост: {self.sent}/{self.total} элементов · {elapsed} с"


async def _album_status_loop(status_message: Message, state: _AlbumStatus) -> None:
    last_text = None
    while True:
        await asyncio.sleep(EDIT_INTERVAL)
        current = state.render()
        if current == last_text:
            continue
        last_text = current
        with suppress(TelegramBadRequest, TelegramRetryAfter), progress_call():
            await status_message.edit_text(current)


async def _terminal_edit(message: Message, text: str) -> None:
    """A flood wait must not silently leave the last progress text on screen."""
    for attempt in range(2):
        try:
            await message.edit_text(text, reply_markup=None)
            return
        except TelegramBadRequest:
            return
        except TelegramRetryAfter as exc:
            if attempt or exc.retry_after > 120:
                logger.warning("Could not update terminal download status: %s", exc)
                return
            await asyncio.sleep(max(0, exc.retry_after))


async def _terminal_delete(message: Message) -> None:
    for attempt in range(2):
        try:
            await message.delete()
            return
        except TelegramBadRequest:
            return
        except TelegramRetryAfter as exc:
            if attempt or exc.retry_after > 120:
                logger.warning("Could not delete terminal download status: %s", exc)
                return
            await asyncio.sleep(max(0, exc.retry_after))


# Set once at startup. A failed Instagram download is a reason to go and ask
# whether the account is still signed in - never an answer to that question, so
# nothing here decides anything. See bot/igsession.py.
_instagram_watch = None


def set_instagram_watch(watch) -> None:
    global _instagram_watch
    _instagram_watch = watch


async def _record(
    db: Database,
    telegram_id: int,
    status: str,
    platform: str,
    media_type: str,
    started: float,
    file_size: int | None = None,
) -> None:
    if (
        status == STATUS_FAILED
        and platform == "instagram"
        and _instagram_watch is not None
    ):
        _instagram_watch.nudge()
    await db.add_conversion(
        telegram_id, status, platform, media_type,
        file_size=file_size,
        processing_time=time.monotonic() - started,
    )
    mark_outcome(status)


@router.message(F.text.startswith(("https://", "http://")))
async def handle_link(
    message: Message,
    db: Database,
    settings: Settings,
    url_cache: UrlCache,
    cancel_registry: CancelRegistry,
) -> None:
    user_id = message.from_user.id
    is_admin = user_id == settings.admin_id

    # Telegram holds updates for a wedged bot and delivers the lot at once when
    # it recovers. Those links are almost never still wanted — the person gave up
    # or sent them again — and downloading all of them is how the bot came back
    # from a freeze straight into another one. Ten minutes is deliberately
    # generous: this is meant to catch a restart backlog, not somebody who
    # waited their turn in a queue.
    if not is_admin and message.date is not None:
        age = (datetime.now(timezone.utc) - message.date).total_seconds()
        # The upper bound is a guard against the machine's own clock rather than
        # against old links: Telegram drops undelivered updates after a day, so
        # anything "older" than that is the host having drifted, and dropping
        # live links because of a bad clock would be far worse than keeping a
        # stale one. NTP is not running on this box.
        if _STALE_AFTER_SECONDS < age < 86400:
            logger.info("Dropping stale link from %s (%.0fs old)", user_id, age)
            await message.answer(
                "⌛ Эта ссылка пролежала слишком долго, пока бот был занят, "
                "и я её пропускаю. Отправьте ещё раз, если всё ещё нужна."
            )
            return

    # Solo mode: the admin reserves the whole pipe for a heavy upload.
    if config.solo_mode and not is_admin:
        await message.answer("⏸ Бот временно занят. Попробуйте через несколько минут.")
        return
    async with _user_gate(user_id, is_admin, message) as allowed:
        if allowed:
            await _run_link(message, db, settings, url_cache, cancel_registry)


async def _run_link(
    message: Message,
    db: Database,
    settings: Settings,
    url_cache: UrlCache,
    cancel_registry: CancelRegistry,
) -> None:
    parts = message.text.strip().split(maxsplit=1)
    url = parts[0]
    time_range = None
    if len(parts) > 1:
        try:
            time_range = parse_time_range(parts[1])
        except ValueError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        if time_range is None:
            await message.answer("⚠️ Для подрезки добавьте после ссылки интервал 01:20-02:40.")
            return
    platform = detect_platform(url)

    user_id = message.from_user.id
    is_admin = user_id == settings.admin_id
    await db.upsert_user(user_id, message.from_user.username)

    # The bot sits in a network that reaches the router, the host's SSH port and
    # the database. Fetching a link that points there would let any user read
    # internal services through the bot and map the network from the errors.
    # A per-share short link hides the post's identity behind a redirect, and the
    # cache cannot recognise the video until it is expanded.
    # Expansion walks the proxy ladder, so ten identical pastes would each pay
    # for it before anything could notice they are the same link.
    raw_url = url
    cached_expansion = _expanded_links.get(raw_url)
    if cached_expansion is not None:
        url = cached_expansion
        _expanded_links.move_to_end(raw_url)
    else:
        url = await expand_short_link(url)
        if url != raw_url:
            _remember_expansion(raw_url, url)
    platform = detect_platform(url)
    if platform == "pinterest" and not re.fullmatch(r"/pin/\d+/?", urlparse(url).path):
        await message.answer("⚠️ Пришлите ссылку на отдельный пин Pinterest, не доску или профиль.")
        return

    try:
        ensure_public_url(url)
    except BlockedAddressError as exc:
        logger.warning("Blocked internal-address link from %s: %s", user_id, exc)
        await message.answer("⚠️ Эта ссылка ведёт не в интернет. Пришлите обычную ссылку.")
        await _record(db, user_id, STATUS_FAILED, platform, "video", time.monotonic())
        return

    # Music services (SoundCloud, Yandex Music, Spotify) and YouTube Music are
    # always audio-only — no video/audio prompt.
    if is_youtube_music(url) or is_audio_only_platform(platform):
        if is_music_album(url):
            await message.answer(
                "⚠️ Скачивание альбомов и плейлистов не поддерживается — "
                "пришлите ссылку на отдельный трек."
            )
            return
        await _run_audio_flow(
            message, db, settings, cancel_registry,
            url, platform, user_id, url_cache.store(url), time_range=time_range,
        )
        return

    # For a full YouTube video the user picks video or audio up front (video on
    # timeout). Shorts are always downloaded as video — no prompt.
    choice_message: Message | None = None
    if platform == "youtube" and not is_youtube_shorts(url):
        choice_token = url_cache.store(url)
        choice, choice_message = await _ask_youtube_choice(message, choice_token)
        if choice == "audio":
            await _run_audio_flow(
                message, db, settings, cancel_registry,
                url, platform, user_id, choice_token, status_message=choice_message,
                time_range=time_range,
            )
            return

    # Short-form sources (no quality menu) keep a single cached copy that is
    # served back instantly. Quality-menu platforms are cached per resolution
    # below, after the user has chosen — so the menu is always offered.
    if time_range is None and not _should_offer_quality(url, platform) and await _send_cached_video(
        message, db, url_cache, url, platform, _user_limit(settings, user_id),
        ("video_validated", "video_validated_capped") if platform == "tiktok"
        else ("video", "video_capped"),
    ):
        if choice_message is not None:
            await _delete_silently(choice_message)
        return

    # Everything above this point is free — a stale link, a cache hit, a link
    # that turned out to be audio. The budget is charged here, where the machine
    # is about to do real work, so it limits the people actually making it work
    # and never the ones being served from something already done. Pasting the
    # same link over and over is bounded by the flood limit in front of every
    # router instead, which is the right shape for it: that costs messages, not
    # downloads.
    if not is_admin:
        wait = _rate_delay(user_id)
        if wait > 0:
            if choice_message is not None:
                await _delete_silently(choice_message)
            await message.answer(
                f"🐢 Не больше {int(_RATE_TOKENS)} загрузок в минуту. "
                f"Следующую приму через {max(1, int(wait))} с — "
                "всё, что уже скачано, по-прежнему отдаётся мгновенно."
            )
            return

    # Carousel/photo posts go straight to gallery-dl: yt-dlp would either fail
    # (TikTok /photo/) or silently drop the photos of a mixed Instagram post.
    if is_photo_post_url(url) and time_range is None:
        album_status = await message.answer("📸 Скачиваю пост…")
        if await _deliver_album(
            message, db, album_status, url, settings, platform, user_id,
            time.monotonic(), url_cache,
        ):
            return
        # Not actually an album (e.g. a plain /p/ video post) — try the video flow.
        await _delete_silently(album_status)

    token = url_cache.store(url)
    if _download_limit(settings, user_id) < 10 * 1024**2:
        await message.answer("⚠️ На сервере недостаточно свободного места для загрузки.")
        return
    if choice_message is not None:
        status_message = choice_message
    else:
        status_message = await message.answer("⏳ Готовлю загрузку, подождите…")
    started = time.monotonic()

    # Resolve page → real media URL where yt-dlp can't (e.g. the-joi-database HLS).
    # The original page URL stays the cache/token key; download_url feeds yt-dlp.
    try:
        download_url = await resolve_download_url(url)
        # The page decides this one (an HLS manifest, a redirect target), so it
        # is as untrusted as the link the user sent.
        ensure_public_url(download_url)
    except BlockedAddressError:
        logger.warning("Resolved URL for %s points at a private address", url)
        await _safe_edit(status_message, "⚠️ Ссылка ведёт не в интернет.")
        await _record(db, user_id, STATUS_FAILED, platform, "video", started)
        return
    except DownloadFailedError as exc:
        await _safe_edit(status_message, f"⚠️ {exc}")
        await _record(db, user_id, STATUS_FAILED, platform, "video", started)
        return

    # Ask which resolution to download (size shown) for everything but short-form.
    format_override: str | None = None
    estimated_size: int | None = None
    cache_key = _video_cache_key(platform)
    # Rule34 has one source file; its excerpt resolution is selected after
    # fetching that file. Other sites expose downloadable quality variants.
    if platform != "rule34" and _should_offer_quality(url, platform):
        try:
            chosen = await _select_quality(
                status_message, token, download_url, settings,
                _download_limit(settings, user_id),
                time_range=time_range,
            )
        except DownloadFailedError as exc:
            await _safe_edit(status_message, f"⚠️ {exc}")
            await _record(
                db, user_id, STATUS_FAILED, platform, "video", started,
                file_size=getattr(exc, "size_bytes", None),
            )
            return
        if chosen is not None:
            estimated_size = chosen.estimated_size
            format_override = quality_selector(chosen)
            cache_key = _video_cache_key(platform, height=chosen.height)
            # Same link + same quality already uploaded → resend instantly.
            if time_range is None and await _send_cached_video(
                message, db, url_cache, url, platform,
                _user_limit(settings, user_id), (cache_key,),
            ):
                await _delete_silently(status_message)
                return

    cancel_event = cancel_registry.register(token)
    await _safe_edit(
        status_message, "⏳ Скачиваю видео, подождите…",
        reply_markup=_cancel_keyboard(token),
    )
    progress = ProgressState()
    progress_task = asyncio.create_task(
        _edit_progress_loop(status_message, progress, "видео", token, cancel_event)
    )
    file_size: int | None = None
    capped = False
    force_proxy = False
    part_limit = _part_upload_limit(settings, user_id)
    try:
        # Admin downloads run immediately; others share the concurrency cap.
        while True:
            try:
                async with _admitted_media(download_video(
                    download_url,
                    settings.cookies_file,
                    max_bytes=_download_limit(settings, user_id),
                    progress=progress,
                    cancel_event=cancel_event,
                    capped=capped,
                    format_override=format_override,
                    force_proxy=force_proxy,
                    ratelimit=_ratelimit_bps(
                        user_id, settings, platform, force_proxy
                    ),
                    time_range=time_range,
                ), is_admin,
                    large_source=_needs_large_video_slot(
                        _should_offer_quality(url, platform), time_range,
                        estimated_size, part_limit,
                    ),
                    large_if_bytes=part_limit,
                    progress=progress, cancel_event=cancel_event,
                ) as media:
                    file_size = media.file_size
                    progress.phase = PHASE_PREPARE
                    progress.started_at = time.monotonic()
                    with_description = _description_capable(url, platform) and bool(
                        media.description and media.description.strip()
                    )
                    if time_range is not None or media.file_size > part_limit:
                        if time_range is not None and platform == "rule34":
                            progress.phase = PHASE_PAUSED
                            try:
                                await _select_quality(
                                    status_message, token, url, settings,
                                    _download_limit(settings, user_id),
                                    time_range=time_range,
                                    options=_source_quality_options(media.height, media.file_size),
                                )
                            finally:
                                progress.phase = PHASE_PREPARE
                        async with _prepare_slots:
                            upload_paths = await prepare_video(
                                media.path, part_limit,
                                None if platform == "youtube" else time_range,
                                cancel_event,
                                force_compatible=time_range is not None,
                            )
                    else:
                        upload_paths = [media.path]
                    _release_source_after_split(media.path, upload_paths)
                    progress.phase = PHASE_UPLOAD
                    progress.downloaded = sum(path.stat().st_size for path in upload_paths)
                    progress.part_count = len(upload_paths)
                    sent = None
                    for part_index, upload_path in enumerate(upload_paths, 1):
                        progress.part_index = part_index
                        if cancel_event.is_set():
                            raise DownloadCancelledError
                        part_size = upload_path.stat().st_size
                        part_caption = _CAPTION
                        part_width, part_height = media.width, media.height
                        if time_range is not None:
                            part_width, part_height, _ = await asyncio.to_thread(
                                video_metadata, upload_path,
                            )
                        if len(upload_paths) > 1:
                            part_caption += (
                                f"\nЧасть {part_index}/{len(upload_paths)}"
                            )
                        one_part = len(upload_paths) == 1
                        part_duration = (
                            media.duration if one_part and time_range is None
                            else None
                        )
                        part_keyboard = (
                            _video_keyboard(token, with_description,
                                            platform == "tiktok")
                            if part_index == 1 else None
                        )

                        def send_part(
                            path=upload_path, caption=part_caption,
                            duration=part_duration, keyboard=part_keyboard,
                            width=part_width, height=part_height,
                        ):
                            return message.answer_video(
                                FSInputFile(path), caption=caption,
                                duration=duration, width=width,
                                height=height, supports_streaming=True,
                                reply_markup=keyboard,
                            )

                        async with _upload_capacity.slot(
                            part_size, is_admin, db, platform
                        ):
                            sent = await _send_prepared_part(
                                send_part, message.bot,
                                _upload_timeout(part_size), upload_path,
                                len(upload_paths) > 1,
                            )
                    # Telegram returns mkv uploads as documents — cache either kind.
                    # Each resolution is cached under its own key; the compressed
                    # fallback gets a separate key so it never shadows a full file.
                    sent_media = sent.video or sent.document
                    if (
                        sent_media is not None
                        and len(upload_paths) == 1
                        and time_range is None
                    ):
                        stored_key = (
                            _video_cache_key(platform, capped=True) if capped else cache_key
                        )
                        await db.store_cached_file(
                            hash_url(url),
                            stored_key,
                            sent_media.file_id,
                            file_size,
                            description=media.description,
                        )
                    if (
                        platform == "tiktok" and sent_media is not None
                        and len(upload_paths) == 1 and time_range is None
                    ):
                        try:
                            _remember_tiktok_original(url, media.original_path or media.path)
                        except OSError:
                            logger.warning("Could not retain TikTok original for file button")
                break
            except OversizedError as exc:
                # Quality was already chosen up front where applicable; here we
                # only reach short-form sources (TikTok/Instagram) or sites with
                # no per-format sizes — offer a one-tap size-capped download.
                if capped or format_override:
                    raise DownloadFailedError(str(exc)) from exc
                if not await _offer_compressed(status_message, progress, token, exc):
                    await _record(
                        db, user_id, STATUS_CANCELLED, platform, "video", started,
                        file_size=exc.size_bytes,
                    )
                    return
                capped = True  # consent received — retry with the size-capped ladder
            except DownloadFailedError as exc:
                # A different exit IP often clears per-post IP/geo/rate blocks.
                if platform == "tiktok" and not isinstance(
                    exc, DownloadCancelledError | FileTooLargeError
                ):
                    # Its exit may have died a moment ago; tell the prober now
                    # so the next user is not routed through the same node.
                    request_tiktok_reprobe()
                if (
                    not isinstance(exc, DownloadCancelledError | FileTooLargeError)
                    and getattr(exc, "retry_via_proxy", False)
                    and not force_proxy
                    and forced_proxy(platform)
                ):
                    logger.info("Retrying %s via proxy after a block error", url)
                    force_proxy = True
                    continue
                raise
    except MediaPreparationError as exc:
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, f"⚠️ {exc}")
        await _record(db, user_id, STATUS_FAILED, platform, "video", started, file_size)
    except DownloadCancelledError:
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, "🚫 Скачивание отменено.")
        await _record(db, user_id, STATUS_CANCELLED, platform, "video", started)
    except DownloadFailedError as exc:
        await _stop_progress(progress_task)
        logger.warning(
            "Download failed: platform=%s error=%s elapsed=%.1fs",
            platform, type(exc).__name__, time.monotonic() - started,
        )
        # The link may be a photo post, which yt-dlp cannot handle (shortlinks
        # hide the post type until yt-dlp resolves them).
        if (
            time_range is None
            and platform in PHOTO_CAPABLE_PLATFORMS
            and not isinstance(exc, FileTooLargeError | ConfirmedVideoError)
        ):
            await _terminal_edit(status_message, "🔍 Видео не нашлось — проверяю, нет ли там фото…")
            album_proxy = bool(getattr(exc, "retry_via_proxy", False) and forced_proxy(platform))
            if await _deliver_album(
                message, db, status_message, url, settings, platform, user_id,
                started, url_cache, album_proxy,
            ):
                return
        if isinstance(exc, NotAVideoPostError):
            # The post was identified correctly and the photo path still came
            # back empty, so repeating "there is no video in it" would name the
            # wrong problem: nothing reached the post, not the post's contents.
            await _terminal_edit(
                status_message,
                "⚠️ Это фото-пост, но скачать его не удалось: "
                "ни один выход не отдал файлы. Попробуйте ещё раз.",
            )
        else:
            await _terminal_edit(status_message, f"⚠️ {exc}")
        await _record(
            db, user_id, STATUS_FAILED, platform, "video", started,
            file_size=getattr(exc, "size_bytes", None),
        )
    except TelegramNetworkError:
        logger.exception("Network error while sending video for %s", url)
        await _stop_progress(progress_task)
        await _terminal_edit(
            status_message,
            "⚠️ Не удалось отправить файл из-за сетевой ошибки. Попробуйте ещё раз.",
        )
        await _record(db, user_id, STATUS_FAILED, platform, "video", started, file_size)
    except Exception:
        logger.exception("Unexpected error while processing %s", url)
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, "❌ Произошла ошибка. Попробуйте позже.")
        await _record(db, user_id, STATUS_FAILED, platform, "video", started, file_size)
    else:
        await _stop_progress(progress_task)
        await _record(db, user_id, STATUS_DONE, platform, "video", started, file_size)
        await _terminal_delete(status_message)
    finally:
        progress_task.cancel()
        cancel_registry.remove(token)


async def _safe_edit(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    with suppress(TelegramBadRequest, TelegramRetryAfter):
        await message.edit_text(text, reply_markup=reply_markup)


def _youtube_choice_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎬 Видео", callback_data=f"{_YT_VIDEO_PREFIX}{token}"
                ),
                InlineKeyboardButton(
                    text="🎵 Аудио", callback_data=f"{_YT_AUDIO_PREFIX}{token}"
                ),
            ]
        ]
    )


async def _ask_youtube_choice(message: Message, token: str) -> tuple[str, Message]:
    """Ask whether to download video or audio; defaults to video on timeout."""
    event = asyncio.Event()
    state = {"choice": "video"}
    _pending_choices[token] = (event, state)
    question = await message.answer(
        "Что скачать?\n\n"
        f"⏱ Если не выбрать за {int(_MEDIA_CHOICE_TIMEOUT)} секунд — скачаю видео.",
        reply_markup=_youtube_choice_keyboard(token),
    )
    try:
        with suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), _MEDIA_CHOICE_TIMEOUT)
    finally:
        _pending_choices.pop(token, None)
    return state["choice"], question


def _should_offer_quality(url: str, platform: str) -> bool:
    """Ask the user for a resolution everywhere except short-form sources.

    Skipped for TikTok, Instagram and YouTube Shorts (always short clips) and,
    by the caller, for the YouTube audio branch. Applies to regular YouTube,
    PornHub, Rule34Video and any other yt-dlp site.
    """
    if platform in ("tiktok", "instagram"):
        return False
    # PornHub shorties resolve to a regular video, so they keep the quality menu.
    return not (platform == "youtube" and is_youtube_shorts(url))


def _source_quality_options(height: int | None, size: int) -> list[QualityOption]:
    """A post with one media file offers only that source resolution."""
    return [QualityOption(height, size)] if height else []


async def _select_quality(
    status_message: Message, token: str, url: str, settings: Settings, limit: int,
    *, time_range: tuple[int, int] | None = None,
    options: list[QualityOption] | None = None,
) -> QualityOption | None:
    """Ask which resolution to download, with the size of each shown.

    Returns the chosen QualityOption, or None to download normally (the site
    reported no per-format sizes). Raises DownloadFailedError when the choice
    cannot fit the user's upload limit.
    """
    local_output = options is not None
    if options is None:
        await _safe_edit(status_message, "🔍 Смотрю доступные качества…")
        options = await probe_quality_options(url, settings.cookies_file)
    if not options:
        if detect_platform(url) == "rule34video":
            raise DownloadFailedError(
                "Не удалось получить доступные качества источника. Отправьте ссылку ещё раз."
            )
        await _safe_edit(status_message, "⏳ Использую максимальное доступное качество источника.")
        return None  # sizes unknown — fall back to a plain best-quality download

    remote_excerpt = time_range is not None and detect_platform(url) == "youtube"

    limit_mb = limit // (1024 * 1024)
    event = asyncio.Event()
    state: dict = {"height": None}
    _pending_choices[token] = (event, state)
    try:
        def fits(option: QualityOption) -> bool:
            return remote_excerpt or option.estimated_size is None or option.estimated_size <= limit

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"{o.label}{'' if fits(o) else ' 🔒'}",
                        callback_data=f"{_QUALITY_PREFIX}{token}:{o.height}",
                    )
                ]
                for o in options
            ]
        )
        explanation = "\nИсточник предоставляет один файл в этом качестве." if local_output else ""
        if time_range is not None:
            if remote_excerpt:
                explanation = (
                    "\nРазмеры указаны для полного исходника. Скачаю только выбранный интервал;"
                    " его размер заранее неизвестен."
                )
            elif not local_output:
                explanation = (
                    "\nРазмеры указаны для полного исходника; отправлю выбранный интервал."
                )
            if any(option.height > 1080 for option in options):
                explanation += "\nГотовый фрагмент — до 1920×1080."
        await _safe_edit(
            status_message,
            f"📺 Выберите качество (⏱ {int(_MEDIA_CHOICE_TIMEOUT)} с —"
            " иначе лучшее, что влезает):" + explanation,
            reply_markup=keyboard,
        )
        try:
            await asyncio.wait_for(event.wait(), _MEDIA_CHOICE_TIMEOUT)
            chosen = next((o for o in options if o.height == state["height"]), None)
        except TimeoutError:
            chosen = next((o for o in options if fits(o)), None) or options[0]

        if chosen is None:
            raise DownloadFailedError(
                f"Видео не влезает в лимит {limit_mb} МБ даже в минимальном качестве."
            )
        if not fits(chosen):
            raise DownloadFailedError(
                f"Это качество весит ~{chosen.estimated_size / (1024 * 1024):.0f} МБ —"
                f" больше лимита {limit_mb} МБ. Выберите вариант поменьше."
            )
        await _safe_edit(
            status_message,
            f"⏳ {'Использую исходник' if local_output else 'Скачиваю'} в {chosen.height}p…",
            reply_markup=_cancel_keyboard(token),
        )
        return chosen
    finally:
        _pending_choices.pop(token, None)


def _compress_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📉 Да, скачать сжатое",
                    callback_data=f"{_COMPRESS_YES_PREFIX}{token}",
                ),
                InlineKeyboardButton(
                    text="❌ Нет",
                    callback_data=f"{_COMPRESS_NO_PREFIX}{token}",
                ),
            ]
        ]
    )


async def _offer_compressed(
    status_message: Message, progress: ProgressState, token: str, exc: OversizedError
) -> bool:
    """Ask whether to download a size-capped version; auto-expires in 10 seconds."""
    event = asyncio.Event()
    state = {"accept": False}
    _pending_choices[token] = (event, state)
    progress.phase = PHASE_PAUSED  # keep the updater away from the question
    try:
        await _safe_edit(
            status_message,
            f"⚖️ {exc}\n\nСкачать в сниженном качестве?"
            f" (выбор активен {int(_COMPRESS_CHOICE_TIMEOUT)} с)",
            reply_markup=_compress_keyboard(token),
        )
        try:
            await asyncio.wait_for(event.wait(), _COMPRESS_CHOICE_TIMEOUT)
        except TimeoutError:
            await _safe_edit(
                status_message, "⌛ Время выбора истекло. Отправьте ссылку ещё раз."
            )
            return False
        if not state["accept"]:
            await _safe_edit(status_message, "🚫 Отменено.")
            return False
        await _safe_edit(
            status_message, "⏳ Скачиваю сжатую версию…",
            reply_markup=_cancel_keyboard(token),
        )
        progress.phase = PHASE_DOWNLOAD
        return True
    finally:
        _pending_choices.pop(token, None)


async def _deliver_single_video(
    message: Message,
    db: Database,
    status_message: Message,
    url: str,
    album,
    platform: str,
    user_id: int,
    started: float,
    url_cache: UrlCache,
    is_admin: bool = False,
    settings: Settings | None = None,
) -> bool:
    """Send one video from the fallback path the same way the video path does."""
    path = album.items[0]
    if platform == "twitter":
        path = await prepare_streaming_mp4(path)
    width, height, duration = await asyncio.to_thread(video_metadata, path)
    token = url_cache.store(url)
    if platform == "rule34" and settings is not None:
        await _select_quality(
            status_message, token, url, settings, _download_limit(settings, user_id),
            options=_source_quality_options(height, path.stat().st_size),
        )
    description = (album.description or "").strip()
    with_description = _description_capable(url, platform) and bool(description)
    if with_description:
        _remember_description(token, description)
    try:
        async with _upload_capacity.slot(
            path.stat().st_size, is_admin, db, platform
        ):
            sent = await _send_with_retries(
                lambda: message.answer_video(
                    FSInputFile(path),
                    caption=_CAPTION,
                    width=width, height=height, duration=duration,
                    supports_streaming=True,
                    reply_markup=_video_keyboard(token, with_description, platform == "tiktok"),
                ),
                message.bot,
                _upload_timeout(path.stat().st_size),
            )
    except TelegramNetworkError:
        logger.exception("Network error while sending the fallback video for %s", url)
        return False
    sent_media = sent.video or sent.document
    if sent_media is not None and platform != "tiktok":
        await db.store_cached_file(
            hash_url(url), _video_cache_key(platform), sent_media.file_id,
            path.stat().st_size, description=description or None,
        )
    await _record(db, user_id, STATUS_DONE, platform, "video", started,
                  file_size=path.stat().st_size)
    await _delete_silently(status_message)
    return True


async def _deliver_album(
    message: Message,
    db: Database,
    status_message: Message,
    url: str,
    settings: Settings,
    platform: str,
    user_id: int,
    started: float,
    url_cache: UrlCache,
    force_proxy: bool = False,
) -> bool:
    """Deliver a photo/carousel post (mixed photos+videos, plus the music track).

    Returns True on success; on False the caller falls back to other flows.
    """
    # This path and the single-video one below it used to take upload capacity
    # with no admin bypass at all, so a carousel or a fallback video from the
    # operator queued behind users like anyone else.
    is_admin = user_id == settings.admin_id
    send_started = False
    album_status = _AlbumStatus()
    status_task = asyncio.create_task(
        _album_status_loop(status_message, album_status), name="album-status"
    )
    try:
        async with _admitted_media(
            download_album(url, settings.cookies_file, force_proxy), is_admin
        ) as album:
            album_status.phase = "prepare"
            # A single video here means this was never a carousel — the video
            # path failed and this is the fallback. Sending it as a media group
            # would silently strip the audio and description buttons and push the
            # caption into a message of its own, so it goes out the normal way.
            single_video = (
                len(album.items) == 1
                and album.items[0].suffix.lower() in (".mp4", ".webm", ".mov")
                and album.music is None
            )
            if single_video:
                await _stop_progress(status_task)
                return await _deliver_single_video(
                    message, db, status_message, url, album, platform,
                    user_id, started, url_cache, is_admin, settings,
                )

            group = []
            group_sizes = []
            documents = []  # photos above Telegram's 10 MB photo limit go as files
            for path in album.items:
                if path.suffix.lower() in (".mp4", ".webm", ".mov"):
                    if platform == "twitter":
                        path = await prepare_streaming_mp4(path)
                    # Without explicit dimensions Telegram guesses from the
                    # container and gets it wrong whenever the file carries a
                    # rotation flag or non-square pixels — the video then plays
                    # stretched. video_metadata reports what the player draws.
                    width, height, duration = await asyncio.to_thread(
                        video_metadata, path
                    )
                    group.append(InputMediaVideo(
                        media=FSInputFile(path),
                        supports_streaming=True,
                        width=width, height=height, duration=duration,
                    ))
                    group_sizes.append(path.stat().st_size)
                elif await asyncio.to_thread(
                    photo_needs_document, path, _PHOTO_MAX_BYTES
                ):
                    documents.append(path)
                else:
                    group.append(InputMediaPhoto(media=FSInputFile(path)))
                    group_sizes.append(path.stat().st_size)

            # aiogram models are frozen — the caption must be set at construction,
            # so it goes on whichever item is sent first.
            caption_left = True
            if group:
                first = group[0]
                group[0] = first.model_copy(update={"caption": _CAPTION})
                caption_left = False

            album_status.total = (
                len(group) + len(documents) + int(album.music is not None)
            )
            album_status.phase = "upload"

            # Telegram accepts at most ten items per media group. A TikTok
            # carousel can contain more; split it without duplicating photos.
            async with _upload_capacity.slot(
                album.total_size + (album.music.stat().st_size if album.music else 0),
                is_admin, db, platform,
            ):
                for offset in range(0, len(group), 10):
                    batch = group[offset:offset + 10]
                    batch_size = sum(group_sizes[offset:offset + len(batch)])
                    send_started = True
                    if len(batch) > 1:
                        await _send_paced(
                            lambda batch=batch: message.answer_media_group(batch),
                            _upload_timeout(batch_size),
                        )
                    elif isinstance(batch[0], InputMediaVideo):
                        item = batch[0]
                        await _send_paced(
                            lambda item=item: message.answer_video(
                                item.media, caption=item.caption,
                                supports_streaming=True, width=item.width,
                                height=item.height, duration=item.duration,
                            ), _upload_timeout(batch_size),
                        )
                    else:
                        item = batch[0]
                        await _send_paced(
                            lambda item=item: message.answer_photo(
                                item.media, caption=item.caption,
                            ), _upload_timeout(batch_size),
                        )
                    album_status.sent += len(batch)
                for path in documents:
                    send_started = True
                    await _send_paced(
                        lambda path=path, caption_left=caption_left: message.answer_document(
                            FSInputFile(path), caption=_CAPTION if caption_left else None,
                        ),
                        _upload_timeout(path.stat().st_size),
                    )
                    album_status.sent += 1
                    caption_left = False
                if album.music is not None:
                    send_started = True
                    await _send_paced(
                        lambda: message.answer_audio(
                            FSInputFile(album.music), caption=_CAPTION
                        ),
                        _upload_timeout(album.music.stat().st_size),
                    )
                    album_status.sent += 1
            total_size = album.total_size
            description = album.description
    except Exception:
        await _stop_progress(status_task)
        logger.info("Album delivery failed for %s", url, exc_info=True)
        if send_started:
            await _safe_edit(
                status_message, "⚠️ Не удалось отправить все части поста. Попробуйте позже."
            )
            await _record(db, user_id, STATUS_FAILED, platform, "album", started)
            return True
        return False
    finally:
        await _stop_progress(status_task)
    # Albums cannot carry inline buttons. Keep the document action available
    # even when a TikTok post has no description.
    has_description = bool(
        _description_capable(url, platform) and description and description.strip()
    )
    if has_description or platform == "tiktok":
        token = url_cache.store(url)
        if has_description:
            _remember_description(token, description.strip())
        await message.answer(
            "📝 У этого поста есть описание:" if has_description else "📁 Медиа поста:",
            reply_markup=_description_keyboard(token, platform == "tiktok")
            if has_description else InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="📁 Файл",
                    callback_data=f"{_TIKTOK_FILE_PREFIX}{token}",
                )
            ]]),
        )
    await _record(db, user_id, STATUS_DONE, platform, "album", started, file_size=total_size)
    await _delete_silently(status_message)
    return True


def _video_cache_key(platform: str, *, height: int | None = None, capped: bool = False) -> str:
    # Old Twitter file_ids can contain Matroska sent as sendVideo. Never reuse
    # those uploads after switching to validated streaming MP4s.
    base = {"tiktok": "video_validated", "twitter": "video_mp4_v1"}.get(platform, "video")
    if height is not None:
        return f"{base}:{height}"
    return f"{base}_capped" if capped else base


async def _send_cached_video(
    message: Message,
    db: Database,
    url_cache: UrlCache,
    url: str,
    platform: str,
    limit: int,
    keys: tuple[str, ...],
) -> bool:
    """Resend a previously uploaded video by its Telegram file_id. Returns True on success.

    Each resolution is cached under its own key (e.g. "video:720"), so a link
    downloaded once at a chosen quality is only served back at that same quality
    — the quality menu is still offered for every new request. A cached file
    bigger than the user's limit is not served to them.
    """
    for cache_key in keys:
        cached = await db.get_cached_file(hash_url(url), cache_key)
        if cached is None:
            continue
        if cached["file_size"] and cached["file_size"] > limit:
            continue
        with_description = _description_capable(url, platform) and bool(
            cached["description"] and cached["description"].strip()
        )
        file_id = cached["file_id"]
        keyboard = _video_keyboard(
            url_cache.store(url), with_description, platform == "tiktok"
        )
        try:
            await _send_paced(lambda file_id=file_id, keyboard=keyboard: message.answer_video(
                file_id,
                caption=_CAPTION,
                reply_markup=keyboard,
            ), 90)
        except TelegramBadRequest:
            # The file_id became invalid — drop it and download normally.
            await db.delete_cached_file(hash_url(url), cache_key)
            continue
        await db.add_conversion(
            message.from_user.id, STATUS_DONE, platform, "video",
            file_size=cached["file_size"], processing_time=0.0,
        )
        return True
    return False


@router.callback_query(F.data.startswith(_CANCEL_CALLBACK_PREFIX))
async def handle_cancel_request(
    callback: CallbackQuery, cancel_registry: CancelRegistry
) -> None:
    if not cancel_registry.cancel(callback.data.removeprefix(_CANCEL_CALLBACK_PREFIX)):
        await callback.answer("Скачивание уже завершено.")
        return
    await callback.answer("Отменяю…")
    # Instant feedback: the worker thread aborts on its next chunk.
    if isinstance(callback.message, Message):
        await _safe_edit(callback.message, "🚫 Отменяю скачивание…")


@router.callback_query(F.data.startswith((_COMPRESS_YES_PREFIX, _COMPRESS_NO_PREFIX)))
async def handle_compress_choice(callback: CallbackQuery) -> None:
    accept = callback.data.startswith(_COMPRESS_YES_PREFIX)
    prefix = _COMPRESS_YES_PREFIX if accept else _COMPRESS_NO_PREFIX
    pending = _pending_choices.get(callback.data.removeprefix(prefix))
    if pending is None:
        await callback.answer("Время выбора уже истекло.")
        return
    event, state = pending
    state["accept"] = accept
    event.set()
    await callback.answer()


@router.callback_query(F.data.startswith(_QUALITY_PREFIX))
async def handle_quality_choice(callback: CallbackQuery) -> None:
    token, _, height = callback.data.removeprefix(_QUALITY_PREFIX).partition(":")
    pending = _pending_choices.get(token)
    if pending is None or not height.isdigit():
        await callback.answer("Время выбора уже истекло.")
        return
    event, state = pending
    state["height"] = int(height)
    event.set()
    await callback.answer()


@router.callback_query(F.data.startswith(_DESCRIPTION_PREFIX))
async def handle_description_request(
    callback: CallbackQuery, settings: Settings, url_cache: UrlCache
) -> None:
    token = callback.data.removeprefix(_DESCRIPTION_PREFIX)
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    await callback.answer("Получаю описание…")
    # Albums keep their caption in memory; videos are re-fetched via yt-dlp.
    description = _album_descriptions.get(token)
    if description is None:
        url = url_cache.get(token)
        if url is None:
            await callback.message.answer("📝 Описание устарело. Отправьте ссылку ещё раз.")
            return
        description = await fetch_description(url, settings.cookies_file)
    if not description or not description.strip():
        await callback.message.answer("📝 Описания нет.")
        return
    text = description.strip()
    if len(text) > 4000:  # Telegram message limit is 4096
        text = text[:4000] + "…"
    await callback.message.answer(f"📝 <b>Описание:</b>\n\n{html.quote(text)}")


@router.callback_query(F.data.startswith((_YT_VIDEO_PREFIX, _YT_AUDIO_PREFIX)))
async def handle_youtube_choice(callback: CallbackQuery) -> None:
    audio = callback.data.startswith(_YT_AUDIO_PREFIX)
    prefix = _YT_AUDIO_PREFIX if audio else _YT_VIDEO_PREFIX
    pending = _pending_choices.get(callback.data.removeprefix(prefix))
    if pending is None:
        await callback.answer("Время выбора уже истекло.")
        return
    event, state = pending
    state["choice"] = "audio" if audio else "video"
    event.set()
    await callback.answer()


@router.callback_query(F.data.startswith(_AUDIO_CALLBACK_PREFIX))
async def handle_audio_request(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    url_cache: UrlCache,
    cancel_registry: CancelRegistry,
) -> None:
    token = callback.data.removeprefix(_AUDIO_CALLBACK_PREFIX)
    url = url_cache.get(token)
    if url is None or not isinstance(callback.message, Message):
        await callback.answer(
            "Ссылка устарела. Отправьте её ещё раз.", show_alert=True
        )
        return
    await callback.answer()
    await _run_audio_flow(
        callback.message, db, settings, cancel_registry,
        url, detect_platform(url), callback.from_user.id, token,
    )


@router.callback_query(F.data.startswith(_TIKTOK_FILE_PREFIX))
async def handle_tiktok_file_request(
    callback: CallbackQuery, db: Database, settings: Settings, url_cache: UrlCache,
) -> None:
    """Send the source media as documents, preserving bytes and image quality."""
    token = callback.data.removeprefix(_TIKTOK_FILE_PREFIX)
    url = url_cache.get(token)
    if url is None or detect_platform(url) != "tiktok" or not isinstance(callback.message, Message):
        await callback.answer("Ссылка устарела. Отправьте её ещё раз.", show_alert=True)
        return
    await callback.answer()
    user_id = callback.from_user.id
    is_admin = user_id == settings.admin_id
    async with _user_gate(user_id, is_admin, callback.message) as allowed:
        if not allowed:
            return
        cached = await db.get_cached_file(hash_url(url), _TIKTOK_DOCUMENT_CACHE_KEY)
        if cached is not None and not is_photo_post_url(url) and (
            not cached["file_size"] or cached["file_size"] <= _user_limit(settings, user_id)
        ):
            try:
                sent = await _send_paced(
                    lambda: callback.message.answer_document(
                        cached["file_id"], caption=_CAPTION,
                        disable_content_type_detection=True,
                    ),
                    90,
                )
            except TelegramBadRequest:
                await db.delete_cached_file(hash_url(url), _TIKTOK_DOCUMENT_CACHE_KEY)
            else:
                if sent.document is not None:
                    await db.add_conversion(user_id, STATUS_DONE, "tiktok", "document",
                                            file_size=cached["file_size"], processing_time=0.0)
                    return
                await db.delete_cached_file(hash_url(url), _TIKTOK_DOCUMENT_CACHE_KEY)
        original_path = None if is_photo_post_url(url) else _tiktok_original_path(url)
        delay = _rate_delay(user_id) if not is_admin and original_path is None else 0
        if delay:
            await callback.message.answer(
                f"⏳ Подождите {int(delay) + 1} сек. перед новой загрузкой."
            )
            return
        status = await callback.message.answer("⏳ Готовлю исходные файлы…")
        started = time.monotonic()
        try:
            async with (nullcontext() if is_admin else _download_slots):
                if original_path is not None:
                    # Lease a second hard link so cache eviction cannot remove
                    # the source while this document is uploading.
                    with tempfile.TemporaryDirectory(prefix="tg-tiktok-document-") as tmpdir:
                        document = Path(tmpdir) / "tiktok.mp4"
                        os.link(original_path, document)
                        total = document.stat().st_size
                        async with _upload_capacity.slot(total, is_admin, db, "tiktok"):
                            sent = await _send_with_retries(
                                lambda: callback.message.answer_document(
                                    FSInputFile(document), caption=_CAPTION,
                                    disable_content_type_detection=True,
                                ), callback.message.bot, _upload_timeout(total),
                            )
                        if sent.document is None:
                            raise DownloadFailedError("Telegram не принял видео как файл.")
                        await db.store_cached_file(
                            hash_url(url), _TIKTOK_DOCUMENT_CACHE_KEY, sent.document.file_id, total,
                        )
                elif is_photo_post_url(url):
                    async with download_album(url, settings.cookies_file) as album:
                        if any(p.stat().st_size > _user_limit(settings, user_id)
                               for p in album.items):
                            raise DownloadFailedError("Один из файлов превышает лимит отправки.")
                        total = album.total_size
                        async with _upload_capacity.slot(total, is_admin, db, "tiktok"):
                            for path in album.items:
                                sent = await _send_paced(
                                    lambda p=path: callback.message.answer_document(
                                        FSInputFile(p), caption=_CAPTION,
                                        disable_content_type_detection=True,
                                    ), _upload_timeout(path.stat().st_size),
                                )
                                if sent.document is None:
                                    raise DownloadFailedError("Telegram не принял фото как файл.")
                else:
                    async with download_video(
                        url, settings.cookies_file,
                        max_bytes=_user_limit(settings, user_id),
                        original_only=True,
                    ) as media:
                        source = media.original_path or media.path
                        total = source.stat().st_size
                        if total > _user_limit(settings, user_id):
                            raise FileTooLargeError(total, _user_limit(settings, user_id))
                        async with _upload_capacity.slot(total, is_admin, db, "tiktok"):
                            sent = await _send_with_retries(
                                lambda: callback.message.answer_document(
                                    FSInputFile(source), caption=_CAPTION,
                                    disable_content_type_detection=True,
                                ), callback.message.bot, _upload_timeout(total),
                            )
                        if sent.document is None:
                            raise DownloadFailedError("Telegram не принял видео как файл.")
                        await db.store_cached_file(
                            hash_url(url), _TIKTOK_DOCUMENT_CACHE_KEY, sent.document.file_id, total,
                        )
            await _record(db, user_id, STATUS_DONE, "tiktok", "document", started, total)
            await _delete_silently(status)
        except DownloadFailedError as exc:
            await _safe_edit(status, f"⚠️ {exc}")
            await _record(db, user_id, STATUS_FAILED, "tiktok", "document", started)
        except TelegramNetworkError:
            logger.exception("Network error while sending TikTok document for %s", url)
            await _safe_edit(status, "⚠️ Не удалось отправить файл. Попробуйте ещё раз.")
            await _record(db, user_id, STATUS_FAILED, "tiktok", "document", started)
        except Exception:
            logger.exception("TikTok document delivery failed for %s", url)
            await _safe_edit(status, "⚠️ Не удалось отправить файл. Попробуйте ещё раз.")
            await _record(db, user_id, STATUS_FAILED, "tiktok", "document", started)


async def _run_audio_flow(
    message: Message,
    db: Database,
    settings: Settings,
    cancel_registry: CancelRegistry,
    url: str,
    platform: str,
    user_id: int,
    token: str,
    status_message: Message | None = None,
    time_range: tuple[int, int] | None = None,
) -> None:
    """Extract and send the audio track; shared by the button and the YouTube choice."""
    cached = None if time_range is not None else await db.get_cached_file(hash_url(url), "audio")
    if cached is not None:
        try:
            await message.answer_audio(cached["file_id"], caption=_CAPTION)
        except TelegramBadRequest:
            await db.delete_cached_file(hash_url(url), "audio")
        else:
            await db.add_conversion(
                user_id, STATUS_DONE, platform, "audio",
                file_size=cached["file_size"], processing_time=0.0,
            )
            if status_message is not None:
                await _delete_silently(status_message)
            return

    cancel_event = cancel_registry.register(token)
    if status_message is None:
        status_message = await message.answer(
            "⏳ Извлекаю аудио, подождите…", reply_markup=_cancel_keyboard(token)
        )
    else:
        await _safe_edit(
            status_message, "⏳ Извлекаю аудио, подождите…",
            reply_markup=_cancel_keyboard(token),
        )

    preview: YandexPreview | None = None
    try:
        download_url = await resolve_download_url(url)
    except DownloadFailedError as exc:
        if platform != "yandexmusic":
            await _terminal_edit(status_message, f"⚠️ {exc}")
            cancel_registry.remove(token)
            return
        try:
            preview = await asyncio.to_thread(resolve_yandex_preview, url)
        except PreviewUnavailableError:
            await _safe_edit(status_message, f"⚠️ {exc}")
            cancel_registry.remove(token)
            return
        download_url = preview.url

    progress = ProgressState()
    progress_task = asyncio.create_task(
        _edit_progress_loop(status_message, progress, "аудио", token, cancel_event)
    )
    started = time.monotonic()
    is_admin = user_id == settings.admin_id
    file_size: int | None = None
    capped = False
    force_proxy = False
    try:
        while True:
            try:
                async with _admitted_media(download_audio(
                    download_url,
                    settings.cookies_file,
                    max_bytes=(
                        _download_limit(settings, user_id)
                        if time_range is not None else _user_limit(settings, user_id)
                    ),
                    progress=progress,
                    cancel_event=cancel_event,
                    capped=capped,
                    force_proxy=force_proxy,
                    ratelimit=_ratelimit_bps(user_id, settings, platform, force_proxy),
                ), is_admin,
                    large_source=time_range is not None,
                    large_if_bytes=_part_upload_limit(settings, user_id),
                    progress=progress, cancel_event=cancel_event,
                ) as media:
                    audio_path = media.path
                    if time_range is not None:
                        progress.phase = PHASE_PREPARE
                        progress.started_at = time.monotonic()
                        async with _prepare_slots:
                            audio_path = await prepare_audio(
                                media.path, time_range, cancel_event
                            )
                    file_size = audio_path.stat().st_size
                    limit = _user_limit(settings, user_id)
                    if file_size > limit:
                        raise FileTooLargeError(file_size, limit)
                    progress.downloaded = file_size
                    progress.phase = PHASE_UPLOAD
                    audio_title = preview.title if preview else media.title
                    audio_duration = preview.duration if preview else media.duration
                    if time_range is not None:
                        start, end = time_range
                        audio_duration = int(min(
                            end - start,
                            max(1, audio_duration - start) if audio_duration else end - start,
                        ))
                    audio_caption = (
                        f"{_CAPTION}\n🎧 Превью Яндекс Музыки (30 секунд)."
                        if preview else _CAPTION
                    )
                    async with _upload_capacity.slot(
                        file_size, is_admin, db, platform
                    ):
                        sent = await _send_with_retries(
                            lambda path=audio_path, title=audio_title, duration=audio_duration,
                                   caption=audio_caption: message.answer_audio(
                                FSInputFile(path),
                                title=title,
                                duration=duration,
                                caption=caption,
                            ),
                            message.bot,
                            _upload_timeout(file_size),
                        )
                    sent_media = sent.audio or sent.document
                    if sent_media is not None and time_range is None:
                        await db.store_cached_file(
                            hash_url(url), "audio_preview" if preview else "audio",
                            sent_media.file_id, file_size,
                        )
                break
            except OversizedError as exc:
                if capped:
                    raise DownloadFailedError(str(exc)) from exc
                capped = True  # audio: retry with a smaller stream, no questions asked
            except DownloadFailedError as exc:
                if (
                    not isinstance(exc, DownloadCancelledError | FileTooLargeError)
                    and getattr(exc, "retry_via_proxy", False)
                    and not force_proxy
                    and forced_proxy(platform)
                ):
                    force_proxy = True
                    continue
                if (
                    platform == "yandexmusic" and preview is None
                    and not isinstance(exc, DownloadCancelledError | FileTooLargeError)
                ):
                    try:
                        preview = await asyncio.to_thread(resolve_yandex_preview, url)
                    except PreviewUnavailableError:
                        logger.info("Official Yandex Music preview unavailable for %s", url)
                    else:
                        logger.info("Using official Yandex Music preview for %s", url)
                        download_url = preview.url
                        force_proxy = False
                        capped = False
                        continue
                raise
    except MediaPreparationError as exc:
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, f"⚠️ {exc}")
        await _record(db, user_id, STATUS_FAILED, platform, "audio", started, file_size)
    except DownloadCancelledError:
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, "🚫 Скачивание отменено.")
        await _record(db, user_id, STATUS_CANCELLED, platform, "audio", started)
    except DownloadFailedError as exc:
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, f"⚠️ {exc}")
        await _record(
            db, user_id, STATUS_FAILED, platform, "audio", started,
            file_size=getattr(exc, "size_bytes", None),
        )
    except TelegramNetworkError:
        logger.exception("Network error while sending audio for %s", url)
        await _stop_progress(progress_task)
        await _terminal_edit(
            status_message,
            "⚠️ Не удалось отправить файл из-за сетевой ошибки. Попробуйте ещё раз.",
        )
        await _record(db, user_id, STATUS_FAILED, platform, "audio", started, file_size)
    except Exception:
        logger.exception("Unexpected error while extracting audio from %s", url)
        await _stop_progress(progress_task)
        await _terminal_edit(status_message, "❌ Произошла ошибка. Попробуйте позже.")
        await _record(db, user_id, STATUS_FAILED, platform, "audio", started, file_size)
    else:
        await _stop_progress(progress_task)
        await _record(db, user_id, STATUS_DONE, platform, "audio", started, file_size)
        await _terminal_delete(status_message)
    finally:
        progress_task.cancel()
        cancel_registry.remove(token)
