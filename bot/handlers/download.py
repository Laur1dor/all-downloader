"""Video download by link, audio extraction and download cancellation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext, suppress
from threading import Event

from aiogram import F, Router, html
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
    OversizedError,
    QualityOption,
    detect_platform,
    download_album,
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
    resolve_download_url,
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
_UPLOAD_TIMEOUT = 600
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

# Machine protection: cap concurrent non-admin downloads and serialise uploads
# so the uplink isn't saturated. The admin bypasses both (priority).
_download_slots = asyncio.Semaphore(3)
_upload_slots = asyncio.Semaphore(1)

# Anti-spam: each non-admin user may have only one link download in flight.
# A user who pastes more links while busy is warned once and then ignored.
_busy_users: set[int] = set()
_warned_users: set[int] = set()


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


async def _send_with_retries(
    send: Callable[[], Awaitable[Message]], attempts: int = 3, delay: float = 2.0
) -> Message:
    """Retry an upload on transient network failures (flaky route to Telegram)."""
    for attempt in range(1, attempts + 1):
        try:
            return await send()
        except TelegramNetworkError:
            if attempt == attempts:
                raise
            logger.warning("Upload attempt %d/%d failed, retrying", attempt, attempts)
            await asyncio.sleep(delay)
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
                        async with (nullcontext() if is_admin else _upload_slots):
                            sent = await _send_with_retries(
                                lambda m=media, wd=with_description: message.answer_video(
                                    FSInputFile(m.path),
                                    caption=_CAPTION,
                                    duration=m.duration,
                                    width=m.width,
                                    height=m.height,
                                    supports_streaming=True,
                                    reply_markup=_video_keyboard(token, wd),
                                )
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
                    if (
                        not isinstance(exc, DownloadCancelledError | FileTooLargeError)
                        and getattr(exc, "retry_via_proxy", False)
                        and not force_proxy
                        and forced_proxy()
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
            album_proxy = bool(getattr(exc, "retry_via_proxy", False) and forced_proxy())
            if await _deliver_album(
                message, db, status_message, url, settings, platform, user_id,
                started, url_cache, album_proxy,
            ):
                return
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
    try:
        async with download_album(url, settings.cookies_file, force_proxy) as album:
            group = []
            documents = []  # photos above Telegram's 10 MB photo limit go as files
            for path in album.items:
                if path.suffix.lower() in (".mp4", ".webm", ".mov"):
                    group.append(InputMediaVideo(media=FSInputFile(path), supports_streaming=True))
                elif photo_needs_document(path, _PHOTO_MAX_BYTES):
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

            async with _upload_slots:
                if group:
                    await message.answer_media_group(group)
                for path in documents:
                    await message.answer_document(
                        FSInputFile(path),
                        caption=_CAPTION if caption_left else None,
                        request_timeout=_UPLOAD_TIMEOUT,
                    )
                    caption_left = False
                if album.music is not None:
                    await message.answer_audio(FSInputFile(album.music), caption=_CAPTION)
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
                        async with (nullcontext() if is_admin else _upload_slots):
                            sent = await _send_with_retries(
                                lambda: message.answer_audio(
                                    FSInputFile(media.path),
                                    title=media.title,
                                    duration=media.duration,
                                    caption=_CAPTION,
                                )
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
                        and forced_proxy()
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
