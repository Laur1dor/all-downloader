"""Video download by link, audio extraction and download cancellation."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from datetime import datetime, timezone
from threading import Event
from typing import Any

import aiohttp
from aiogram import Bot, F, Router, html
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
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
    DownloadCancelledError,
    DownloadFailedError,
    FileTooLargeError,
    NotAVideoPostError,
    OversizedError,
    QualityOption,
    detect_platform,
    download_album,
    expand_short_link,
    download_audio,
    download_video,
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
from bot.progress import (
    EDIT_INTERVAL,
    PHASE_DOWNLOAD,
    PHASE_PAUSED,
    PHASE_UPLOAD,
    CancelRegistry,
    ProgressState,
    render_progress,
)
from bot.proxy import forced_proxy, proxy_for
from bot.runtime import config
from bot.urlcache import UrlCache
from bot.urlguard import BlockedAddressError, ensure_public_url

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


def _remember_description(token: str, description: str) -> None:
    _album_descriptions[token] = description
    while len(_album_descriptions) > 500:
        _album_descriptions.popitem(last=False)

# Machine protection: cap concurrent non-admin downloads. The admin bypasses it.
_download_slots = asyncio.Semaphore(3)

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


def _video_keyboard(token: str, with_description: bool = False) -> InlineKeyboardMarkup:
    """Buttons under a delivered video: audio always, description when present."""
    buttons = [
        InlineKeyboardButton(
            text="🎵 Скачать аудио", callback_data=f"{_AUDIO_CALLBACK_PREFIX}{token}"
        )
    ]
    if with_description:
        buttons.append(
            InlineKeyboardButton(
                text="📝 Описание", callback_data=f"{_DESCRIPTION_PREFIX}{token}"
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _description_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📝 Показать описание",
                    callback_data=f"{_DESCRIPTION_PREFIX}{token}",
                )
            ]
        ]
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

    The deadline is applied here rather than at the call site because the
    answer_* shortcuts only build a method object and accept anything else as an
    extra model field, so a request_timeout handed to them is silently dropped.
    The bot call is the one place that honours it.
    """
    for attempt in range(1, attempts + 1):
        try:
            return await bot(build(), request_timeout=timeout)
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
        with suppress(TelegramBadRequest, TelegramRetryAfter):
            await status_message.edit_text(text, reply_markup=markup)


async def _record(
    db: Database,
    telegram_id: int,
    status: str,
    platform: str,
    media_type: str,
    started: float,
    file_size: int | None = None,
) -> None:
    await db.add_conversion(
        telegram_id, status, platform, media_type,
        file_size=file_size,
        processing_time=time.monotonic() - started,
    )


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
    url = message.text.strip()
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
            url, platform, user_id, url_cache.store(url),
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
            )
            return

    # Short-form sources (no quality menu) keep a single cached copy that is
    # served back instantly. Quality-menu platforms are cached per resolution
    # below, after the user has chosen — so the menu is always offered.
    if not _should_offer_quality(url, platform) and await _send_cached_video(
        message, db, url_cache, url, platform, _user_limit(settings, user_id),
        ("video", "video_capped"),
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
    if is_photo_post_url(url):
        album_status = await message.answer("📸 Скачиваю пост…")
        if await _deliver_album(
            message, db, album_status, url, settings, platform, user_id,
            time.monotonic(), url_cache,
        ):
            return
        # Not actually an album (e.g. a plain /p/ video post) — try the video flow.
        await _delete_silently(album_status)

    token = url_cache.store(url)
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
    cache_key = "video"
    if _should_offer_quality(url, platform):
        try:
            chosen = await _select_quality(
                status_message, token, download_url, settings, _user_limit(settings, user_id)
            )
        except DownloadFailedError as exc:
            await _safe_edit(status_message, f"⚠️ {exc}")
            await _record(
                db, user_id, STATUS_FAILED, platform, "video", started,
                file_size=getattr(exc, "size_bytes", None),
            )
            return
        if chosen is not None:
            format_override = quality_selector(chosen)
            cache_key = f"video:{chosen.height}"
            # Same link + same quality already uploaded → resend instantly.
            if await _send_cached_video(
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
    try:
        # Admin downloads run immediately; others share the concurrency cap.
        async with (nullcontext() if is_admin else _download_slots):
            while True:
                try:
                    async with download_video(
                        download_url,
                        settings.cookies_file,
                        max_bytes=_user_limit(settings, user_id),
                        progress=progress,
                        cancel_event=cancel_event,
                        capped=capped,
                        format_override=format_override,
                        force_proxy=force_proxy,
                        ratelimit=_ratelimit_bps(user_id, settings, platform, force_proxy),
                    ) as media:
                        file_size = media.file_size
                        progress.downloaded = file_size
                        progress.phase = PHASE_UPLOAD  # the updater shows the marquee bar
                        with_description = _description_capable(url, platform) and bool(
                            media.description and media.description.strip()
                        )
                        async with _upload_capacity.slot(
                            media.file_size, is_admin, db, platform
                        ):
                            sent = await _send_with_retries(
                                lambda m=media, wd=with_description: message.answer_video(
                                    FSInputFile(m.path),
                                    caption=_CAPTION,
                                    duration=m.duration,
                                    width=m.width,
                                    height=m.height,
                                    supports_streaming=True,
                                    reply_markup=_video_keyboard(token, wd),
                                ),
                                message.bot,
                                _upload_timeout(media.file_size),
                            )
                        # Telegram returns mkv uploads as documents — cache either kind.
                        # Each resolution is cached under its own key; the compressed
                        # fallback gets a separate key so it never shadows a full file.
                        sent_media = sent.video or sent.document
                        if sent_media is not None:
                            await db.store_cached_file(
                                hash_url(url),
                                "video_capped" if capped else cache_key,
                                sent_media.file_id,
                                file_size,
                                description=media.description,
                            )
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
    except DownloadCancelledError:
        await _safe_edit(status_message, "🚫 Скачивание отменено.")
        await _record(db, user_id, STATUS_CANCELLED, platform, "video", started)
    except DownloadFailedError as exc:
        # The link may be a photo post, which yt-dlp cannot handle (shortlinks
        # hide the post type until yt-dlp resolves them).
        if platform in PHOTO_CAPABLE_PLATFORMS and not isinstance(exc, FileTooLargeError):
            await _safe_edit(status_message, "🔍 Видео не нашлось — проверяю, нет ли там фото…")
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
            await _safe_edit(
                status_message,
                "⚠️ Это фото-пост, но скачать его не удалось: "
                "ни один выход не отдал файлы. Попробуйте ещё раз.",
            )
        else:
            await _safe_edit(status_message, f"⚠️ {exc}")
        await _record(
            db, user_id, STATUS_FAILED, platform, "video", started,
            file_size=getattr(exc, "size_bytes", None),
        )
    except TelegramNetworkError:
        logger.exception("Network error while sending video for %s", url)
        await _safe_edit(
            status_message,
            "⚠️ Не удалось отправить файл из-за сетевой ошибки. Попробуйте ещё раз.",
        )
        await _record(db, user_id, STATUS_FAILED, platform, "video", started, file_size)
    except Exception:
        logger.exception("Unexpected error while processing %s", url)
        await _safe_edit(status_message, "❌ Произошла ошибка. Попробуйте позже.")
        await _record(db, user_id, STATUS_FAILED, platform, "video", started, file_size)
    else:
        await _record(db, user_id, STATUS_DONE, platform, "video", started, file_size)
        await _delete_silently(status_message)
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


async def _select_quality(
    status_message: Message, token: str, url: str, settings: Settings, limit: int
) -> QualityOption | None:
    """Ask which resolution to download, with the size of each shown.

    Returns the chosen QualityOption, or None to download normally (the site
    reported no per-format sizes). Raises DownloadFailedError when the choice
    cannot fit the user's upload limit.
    """
    await _safe_edit(status_message, "🔍 Смотрю доступные качества…")
    options = await probe_quality_options(url, settings.cookies_file)
    if not options:
        return None  # sizes unknown — fall back to a plain best-quality download

    limit_mb = limit // (1024 * 1024)
    event = asyncio.Event()
    state: dict = {"height": None}
    _pending_choices[token] = (event, state)
    try:
        def fits(option: QualityOption) -> bool:
            return option.estimated_size is None or option.estimated_size <= limit

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
        await _safe_edit(
            status_message,
            f"📺 Выберите качество (⏱ {int(_MEDIA_CHOICE_TIMEOUT)} с —"
            " иначе лучшее, что влезает):",
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
        if chosen.estimated_size is not None and chosen.estimated_size > limit:
            raise DownloadFailedError(
                f"Это качество весит ~{chosen.estimated_size / (1024 * 1024):.0f} МБ —"
                f" больше лимита {limit_mb} МБ. Выберите вариант поменьше."
            )
        await _safe_edit(
            status_message, f"⏳ Скачиваю в {chosen.height}p…",
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
) -> bool:
    """Send one video from the fallback path the same way the video path does."""
    path = album.items[0]
    width, height, duration = await asyncio.to_thread(video_metadata, path)
    token = url_cache.store(url)
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
                    reply_markup=_video_keyboard(token, with_description),
                ),
                message.bot,
                _upload_timeout(path.stat().st_size),
            )
    except TelegramNetworkError:
        logger.exception("Network error while sending the fallback video for %s", url)
        return False
    sent_media = sent.video or sent.document
    if sent_media is not None:
        await db.store_cached_file(
            hash_url(url), "video", sent_media.file_id,
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
    try:
        async with download_album(url, settings.cookies_file, force_proxy) as album:
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
                return await _deliver_single_video(
                    message, db, status_message, url, album, platform,
                    user_id, started, url_cache, is_admin,
                )

            group = []
            documents = []  # photos above Telegram's 10 MB photo limit go as files
            for path in album.items:
                if path.suffix.lower() in (".mp4", ".webm", ".mov"):
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
                elif await asyncio.to_thread(
                    photo_needs_document, path, _PHOTO_MAX_BYTES
                ):
                    documents.append(path)
                else:
                    group.append(InputMediaPhoto(media=FSInputFile(path)))

            # aiogram models are frozen — the caption must be set at construction,
            # so it goes on whichever item is sent first.
            caption_left = True
            if group:
                first = group[0]
                group[0] = first.model_copy(update={"caption": _CAPTION})
                caption_left = False

            # Up to twelve separate sends happen under one slot here; each one
            # now has a deadline of its own, so a wedged album cannot hold the
            # upload queue for the sum of twelve blanket timeouts.
            async with _upload_capacity.slot(
                sum(p.stat().st_size for p in album.items),
                is_admin, db, platform,
            ):
                if group:
                    await message.bot(
                        message.answer_media_group(group),
                        request_timeout=_upload_timeout(
                            sum(p.stat().st_size for p in album.items)
                        ),
                    )
                for path in documents:
                    await message.bot(
                        message.answer_document(
                            FSInputFile(path),
                            caption=_CAPTION if caption_left else None,
                        ),
                        request_timeout=_upload_timeout(path.stat().st_size),
                    )
                    caption_left = False
                if album.music is not None:
                    await message.bot(
                        message.answer_audio(
                            FSInputFile(album.music), caption=_CAPTION
                        ),
                        request_timeout=_upload_timeout(album.music.stat().st_size),
                    )
            total_size = album.total_size
            description = album.description
    except Exception:
        logger.info("Album delivery failed for %s", url, exc_info=True)
        return False
    # Albums can't carry inline buttons, so offer the caption via a follow-up.
    if _description_capable(url, platform) and description and description.strip():
        token = url_cache.store(url)
        _remember_description(token, description.strip())
        await message.answer(
            "📝 У этого поста есть описание:",
            reply_markup=_description_keyboard(token),
        )
    await _record(db, user_id, STATUS_DONE, platform, "album", started, file_size=total_size)
    await _delete_silently(status_message)
    return True


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
        try:
            await message.answer_video(
                cached["file_id"],
                caption=_CAPTION,
                reply_markup=_video_keyboard(url_cache.store(url), with_description),
            )
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
) -> None:
    """Extract and send the audio track; shared by the button and the YouTube choice."""
    cached = await db.get_cached_file(hash_url(url), "audio")
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

    try:
        download_url = await resolve_download_url(url)
    except DownloadFailedError as exc:
        await _safe_edit(status_message, f"⚠️ {exc}")
        cancel_registry.remove(token)
        return

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
        async with (nullcontext() if is_admin else _download_slots):
            while True:
                try:
                    async with download_audio(
                        download_url,
                        settings.cookies_file,
                        max_bytes=_user_limit(settings, user_id),
                        progress=progress,
                        cancel_event=cancel_event,
                        capped=capped,
                        force_proxy=force_proxy,
                        ratelimit=_ratelimit_bps(user_id, settings, platform, force_proxy),
                    ) as media:
                        file_size = media.file_size
                        progress.downloaded = file_size
                        progress.phase = PHASE_UPLOAD
                        async with _upload_capacity.slot(
                            media.file_size, is_admin, db, platform
                        ):
                            sent = await _send_with_retries(
                                lambda: message.answer_audio(
                                    FSInputFile(media.path),
                                    title=media.title,
                                    duration=media.duration,
                                    caption=_CAPTION,
                                ),
                                message.bot,
                                _upload_timeout(media.file_size),
                            )
                        sent_media = sent.audio or sent.document
                        if sent_media is not None:
                            await db.store_cached_file(
                                hash_url(url), "audio", sent_media.file_id, file_size
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
                    raise
    except DownloadCancelledError:
        await _safe_edit(status_message, "🚫 Скачивание отменено.")
        await _record(db, user_id, STATUS_CANCELLED, platform, "audio", started)
    except DownloadFailedError as exc:
        await _safe_edit(status_message, f"⚠️ {exc}")
        await _record(
            db, user_id, STATUS_FAILED, platform, "audio", started,
            file_size=getattr(exc, "size_bytes", None),
        )
    except TelegramNetworkError:
        logger.exception("Network error while sending audio for %s", url)
        await _safe_edit(
            status_message,
            "⚠️ Не удалось отправить файл из-за сетевой ошибки. Попробуйте ещё раз.",
        )
        await _record(db, user_id, STATUS_FAILED, platform, "audio", started, file_size)
    except Exception:
        logger.exception("Unexpected error while extracting audio from %s", url)
        await _safe_edit(status_message, "❌ Произошла ошибка. Попробуйте позже.")
        await _record(db, user_id, STATUS_FAILED, platform, "audio", started, file_size)
    else:
        await _record(db, user_id, STATUS_DONE, platform, "audio", started, file_size)
        await _delete_silently(status_message)
    finally:
        progress_task.cancel()
        cancel_registry.remove(token)
