"""Media downloading built on yt-dlp.

Quality policy: always take the highest available resolution/FPS and, among
equal-quality formats, prefer H.264/AAC so videos play inline in every
Telegram client. Streams are merged/remuxed by ffmpeg but never re-encoded.
A size-capped retry happens only when the best-quality file cannot fit into
the Telegram Bot API upload limit at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtdlpDownloadError
from yt_dlp.utils import YoutubeDLError

from bot.progress import ProgressState
from bot.proxy import forced_proxy, proxy_for

logger = logging.getLogger(__name__)

TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024  # Bot API hard limit for uploads

_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
# h264+aac first: phones play other codecs (vp9/av1) as a frozen frame with sound,
# because mobile Telegram relies on hardware decoding. Among h264 formats the
# highest resolution and FPS still win; vp9/av1 are used only if no h264 exists.
_VIDEO_FORMAT_SORT = ["vcodec:h264", "res", "fps", "acodec:aac"]
# m4a (AAC) first: Telegram's sendAudio officially supports MP3/M4A only, and
# picking it directly avoids any re-encoding; otherwise take the best stream as is.
_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

# Used only when the best-quality file exceeds the Telegram limit. Size filters
# drop formats with unknown sizes, so a height ladder and finally the smallest
# available format act as fallbacks; the post-download size check still guards.
_VIDEO_FORMAT_CAPPED = (
    "bv*[filesize_approx<40M]+ba[filesize_approx<8M]/b[filesize_approx<48M]"
    "/bv*[height<=480]+ba/b[height<=480]/w"
)
_AUDIO_FORMAT_CAPPED = (
    "ba[ext=m4a][filesize_approx<48M]/ba[filesize_approx<48M]/b[filesize_approx<48M]/wa/w"
)

_PLATFORM_DOMAINS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "pornhub.com": "pornhub",
    "rule34video.com": "rule34video",
    "rule34.xxx": "rule34",
    "the-joi-database.com": "joidb",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "soundcloud.com": "soundcloud",
    "music.yandex.ru": "yandexmusic",
    "music.yandex.com": "yandexmusic",
    "music.yandex.by": "yandexmusic",
    "music.yandex.kz": "yandexmusic",
    "open.spotify.com": "spotify",
    "spotify.com": "spotify",
}

# Music services: always delivered as an audio file, never a video prompt.
AUDIO_ONLY_PLATFORMS = ("soundcloud", "yandexmusic", "spotify")


def is_audio_only_platform(platform: str) -> bool:
    return platform in AUDIO_ONLY_PLATFORMS


def is_music_album(url: str) -> bool:
    """Multi-track music links (albums/playlists) — refused: downloading a whole
    album is a long, many-file job, so only single tracks are accepted."""
    platform = detect_platform(url)
    path = urlparse(url).path.lower()
    if platform == "spotify":
        return "/album/" in path or "/playlist/" in path
    if platform == "soundcloud":
        return "/sets/" in path
    if platform == "yandexmusic":
        # /album/123/track/456 is one track; /album/123 alone is the whole album.
        return (
            "/playlist" in path
            or "/users/" in path
            or ("/album/" in path and "/track/" not in path)
        )
    return False

# Hosts with a misconfigured TLS cert that nonetheless serve valid media.
_NOCHECKCERT_HOSTS = ("the-joi-database.com",)

# Platforms whose links may be image/photo posts that yt-dlp cannot handle.
PHOTO_CAPABLE_PLATFORMS = ("instagram", "tiktok", "rule34", "twitter")


class DownloadFailedError(Exception):
    """Download failure with a user-facing (Russian) message.

    retry_via_proxy marks errors (IP block, geo, rate-limit) that a different
    exit IP — i.e. the VLESS proxy — is likely to get past.
    """

    def __init__(self, message: str, *, retry_via_proxy: bool = False) -> None:
        super().__init__(message)
        self.retry_via_proxy = retry_via_proxy


class DownloadCancelledError(DownloadFailedError):
    """The user pressed the cancel button while downloading."""

    def __init__(self) -> None:
        super().__init__("Скачивание отменено.")


class FileTooLargeError(DownloadFailedError):
    """Final verdict: even the smallest available format exceeds the limit."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.size_mb = size_bytes / (1024 * 1024)
        super().__init__(
            f"Файл слишком большой: {self.size_mb:.1f} МБ."
            f" Лимит — {limit_bytes // (1024 * 1024)} МБ."
        )


class OversizedError(DownloadFailedError):
    """Best quality exceeds the limit; a size-capped attempt may still fit.

    Raised before (or as soon as) the oversize is detected, so nothing big is
    ever downloaded in vain. The handler decides whether to retry capped.
    """

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.size_mb = size_bytes / (1024 * 1024)
        self.limit_mb = limit_bytes // (1024 * 1024)
        super().__init__(
            f"Исходник весит ~{self.size_mb:.0f} МБ — больше лимита {self.limit_mb} МБ."
        )


@dataclass(frozen=True, slots=True)
class Media:
    path: Path
    title: str
    file_size: int
    duration: int | None
    width: int | None
    height: int | None
    description: str | None = None


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, platform in _PLATFORM_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return "other"


def is_youtube_shorts(url: str) -> bool:
    return "/shorts/" in urlparse(url).path


def is_youtube_music(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "music.youtube.com"


# the-joi-database.com embeds its HLS stream in page JS; the generic extractor
# misses it, so the .m3u8 manifest URL is scraped out and handed to yt-dlp.
_M3U8_RE = re.compile(r'https?://[^"\' \\]+\.m3u8[^"\' \\]*')


def _curl_get(url: str, *, proxy: str | None = None, **kwargs):
    """A browser-like HTTP GET via curl_cffi (handles TLS fingerprinting + SOCKS)."""
    from curl_cffi import requests as cffi_requests

    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return cffi_requests.get(url, **kwargs)


def _fetch_html(url: str, proxy: str | None = None, attempts: int = 3) -> str:
    """Fetch a page as a real browser would (impersonated TLS + optional proxy).

    Some sites block plain-Python TLS fingerprints and have a broken cert on the
    bare domain, so impersonation + relaxed verification are used.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = _curl_get(
                url, proxy=proxy, impersonate="chrome", timeout=30, verify=False
            )
            response.raise_for_status()
            return response.text
        except Exception as exc:  # transient network/TLS — retry
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(1.5)
    raise DownloadFailedError("Не удалось открыть страницу. Попробуйте ещё раз.") from last_exc


def _resolve_joidb_sync(url: str, proxy: str | None) -> str:
    html = _fetch_html(url, proxy=proxy)
    match = _M3U8_RE.search(html)
    if not match:
        raise DownloadFailedError("Не удалось найти видео на странице.")
    return match.group(0).replace("&amp;", "&")


def is_pornhub_shortie(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("pornhub.com") and "/shorties/" in urlparse(url).path


def _pornhub_shortie_to_video(url: str) -> str:
    """A shortie id doubles as a regular viewkey, which yt-dlp handles fully
    (formats, age gate, CDN) — unlike the /shorties/ page itself."""
    shortie_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return f"https://www.pornhub.com/view_video.php?viewkey={shortie_id}"


def _yandex_to_ytsearch_sync(url: str) -> str:
    """Yandex Music's yt-dlp extractor is broken upstream (its web API endpoint
    returns 404), so the track title is scraped from the page and matched on
    YouTube instead — same pragmatic approach spotdl uses for Spotify.
    """
    html_text = _fetch_html(url, proxy=proxy_for("yandexmusic"))
    match = re.search(r"<title>([^<]+)</title>", html_text)
    query = ""
    if match:
        query = re.split(r"\s+(?:Listen online|слушать онлайн)", match.group(1))[0].strip()
    if not query:
        raise DownloadFailedError("Не удалось определить трек Yandex Music.")
    return f"ytsearch1:{query}"


async def resolve_download_url(url: str) -> str:
    """Map a page URL to the real media URL when yt-dlp can't do it itself.

    the-joi-database.com /watch/ pages embed HLS in page JS; PornHub /shorties/
    map to a regular viewkey; Yandex Music maps to a YouTube search. Every other
    URL is returned unchanged.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("the-joi-database.com") and "/watch/" in urlparse(url).path:
        return await asyncio.to_thread(_resolve_joidb_sync, url, proxy_for("joidb"))
    if is_pornhub_shortie(url):
        return _pornhub_shortie_to_video(url)
    if detect_platform(url) == "yandexmusic":
        return await asyncio.to_thread(_yandex_to_ytsearch_sync, url)
    return url


# Sites that block by TLS fingerprint (PornHub/Cloudflare/Rule34Video) need
# browser impersonation, which requires the optional curl_cffi backend.
_IMPERSONATE_HOSTS = ("pornhub.com", "rule34video.com")


def _impersonate_target():
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return ImpersonateTarget("chrome")
    except Exception:
        return None


def _base_options(cookies_file: Path | None, url: str = "", force_proxy: bool = False) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "restrictfilenames": True,
    }
    if cookies_file is not None:
        options["cookiefile"] = str(cookies_file)

    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _IMPERSONATE_HOSTS):
        target = _impersonate_target()
        if target is not None:
            options["impersonate"] = target
    if any(host == h or host.endswith("." + h) for h in _NOCHECKCERT_HOSTS):
        options["nocheckcertificate"] = True
    _plat = detect_platform(url) if url else ""
    proxy = forced_proxy(_plat) if force_proxy else (proxy_for(_plat) if url else None)
    if proxy:
        options["proxy"] = proxy
    return options


def _progress_hook(
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    size_guard: dict | None = None,
):
    """Build a yt-dlp hook; runs in the worker thread on every downloaded chunk.

    With a size_guard ({"limit": int, "tripped": int|None}) the download is
    aborted as soon as the reported total exceeds the limit, instead of
    finishing a file that could never be sent.
    """

    def hook(data: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if size_guard is not None and total and total > size_guard["limit"]:
            size_guard["tripped"] = int(total)
            raise OversizedError(int(total), size_guard["limit"])
        if progress is None:
            return
        progress.downloaded = int(data.get("downloaded_bytes") or 0)
        progress.total = total
        progress.speed = data.get("speed")
        progress.eta = data.get("eta")

    return hook


# Substrings that mark a retryable error. HTTP codes are matched with the
# "http error" prefix so a bare "502" inside a video id/URL isn't a false hit.
_TRANSIENT_MARKERS = (
    "tls", "curl", "timed out", "timeout", "connection", "reset",
    "temporarily", "http error 5", "http error 429", "too many requests",
)


def _extract_info(options: dict, url: str, *, download: bool, attempts: int = 3) -> dict | None:
    """extract_info with retries on transient network/TLS errors.

    curl_cffi (browser impersonation) occasionally raises a TLS error under
    concurrency; a short retry almost always succeeds.
    """
    last_exc: YtdlpDownloadError | None = None
    for attempt in range(attempts):
        try:
            with YoutubeDL(options) as ydl:
                return ydl.extract_info(url, download=download)
        except YtdlpDownloadError as exc:
            last_exc = exc
            transient = any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)
            if attempt + 1 < attempts and transient:
                logger.info("Transient extract error (retry %d): %s", attempt + 1, exc)
                time.sleep(1.5)
                continue
            raise
        except DownloadFailedError:
            raise  # our own typed errors (Oversized/FileTooLarge/Cancelled) pass through
        except Exception as exc:
            # Some yt-dlp extractors raise raw TypeError/KeyError on blocked or
            # malformed responses (e.g. Yandex Music's anti-bot page). Convert to
            # a friendly error so it never crashes the handler — but log it.
            logger.exception("yt-dlp extractor crashed for %s", url)
            raise DownloadFailedError(
                "Не удалось обработать ссылку — источник недоступен или блокирует запросы.",
                retry_via_proxy=_is_block_error(exc),
            ) from exc
    if last_exc is not None:
        raise last_exc
    return None


def _download_sync(url: str, options: dict) -> tuple[Path, dict]:
    info = _extract_info(options, url, download=True)
    if info is None:
        raise DownloadFailedError("Не удалось скачать медиа по этой ссылке.")
    if info.get("entries"):
        info = info["entries"][0]

    # requested_downloads holds the final path even after merge/extract postprocessing.
    downloads = info.get("requested_downloads") or []
    filepath = downloads[0].get("filepath") if downloads else None
    if not filepath or not Path(filepath).is_file():
        raise DownloadFailedError("Не удалось скачать медиа по этой ссылке.")
    return Path(filepath), info


# Error signatures that a different exit IP (the VLESS proxy) is likely to clear.
_BLOCK_MARKERS = (
    "ip address is blocked", "403", "forbidden", "not available in your country",
    "geo restrict", "geo-restrict", "rate-limit", "too many requests",
)


def _is_block_error(exc: YtdlpDownloadError) -> bool:
    return any(marker in str(exc).lower() for marker in _BLOCK_MARKERS)


def _user_message(exc: YtdlpDownloadError) -> str:
    text = str(exc).lower()
    if "ip address is blocked" in text or "not available in your country" in text:
        return "Доступ к этому контенту заблокирован для сервера."
    if "private" in text or "login" in text or "cookies" in text or "rate-limit" in text:
        return "Контент недоступен: он приватный или требует входа в аккаунт."
    if "unsupported url" in text:
        return "Эта ссылка не поддерживается."
    if "unavailable" in text or "removed" in text or "not exist" in text:
        return "Видео недоступно или удалено."
    return "Не удалось скачать медиа по этой ссылке."


@dataclass(frozen=True, slots=True)
class QualityOption:
    """An available resolution with the estimated download size (if known)."""

    height: int
    estimated_size: int | None
    format_id: str | None = None

    @property
    def label(self) -> str:
        if self.estimated_size is None:
            return f"{self.height}p"
        return f"{self.height}p · ~{self.estimated_size / (1024 * 1024):.0f} МБ"


def quality_format(height: int) -> str:
    """Format selector for a resolution by height (works when `height` is set)."""
    return (
        f"bv*[height={height}][vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*[height={height}]+ba"
        f"/bv*[height<={height}]+ba/b[height<={height}]"
    )


def quality_selector(option: QualityOption) -> str:
    """Exact format selector for a chosen QualityOption.

    Prefers the exact format_id (works even when the extractor omits `height`,
    e.g. Rule34Video) and adds best audio for video-only streams; falls back to
    a height-based match.
    """
    if option.format_id:
        return (
            f"{option.format_id}+ba/{option.format_id}"
            f"/{quality_format(option.height)}"
        )
    return quality_format(option.height)


def _format_size(fmt: dict, duration: float | None = None) -> int | None:
    """Exact size if reported, otherwise estimated from bitrate × duration."""
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size:
        return size
    bitrate_kbps = fmt.get("tbr") or ((fmt.get("vbr") or 0) + (fmt.get("abr") or 0))
    if bitrate_kbps and duration:
        return int(bitrate_kbps * 1000 / 8 * duration)
    return None


def _format_height(fmt: dict) -> int | None:
    """Resolution of a format, derived from height or a quality/note string.

    Some extractors (e.g. Rule34Video) put the resolution only in a `quality`
    or `format_note` string like "1080" instead of the numeric `height` field.
    """
    height = fmt.get("height")
    if height:
        return int(height)
    for key in ("quality", "format_note", "format_id", "format"):
        match = re.search(r"(\d{3,4})", str(fmt.get(key) or ""))
        if match and 144 <= int(match.group(1)) <= 4320:
            return int(match.group(1))
    return None


def _remote_size(url: str, referer: str | None = None, proxy: str | None = None) -> int | None:
    """Content-Length of a direct media URL via curl_cffi (HEAD, then ranged GET)."""
    from curl_cffi import requests as cffi_requests

    headers = {"Referer": referer} if referer else {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        head = cffi_requests.head(
            url, impersonate="chrome", timeout=15, headers=headers, proxies=proxies
        )
        length = head.headers.get("Content-Length")
        if length and str(length).isdigit():
            return int(length)
        ranged = cffi_requests.get(
            url, impersonate="chrome", timeout=15, stream=True, proxies=proxies,
            headers={**headers, "Range": "bytes=0-0"},
        )
        content_range = ranged.headers.get("Content-Range")
        ranged.close()
        if content_range and "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                return int(total)
    except Exception:
        return None
    return None


def _probe_quality_options_sync(
    url: str, options: dict, proxy: str | None = None
) -> list[QualityOption]:
    """List all available resolutions with their estimated sizes, best first."""
    probe_options = {k: v for k, v in options.items() if k != "format"}
    info = _extract_info(probe_options | {"skip_download": True}, url, download=False)
    if info is None:
        return []
    if info.get("entries"):
        info = info["entries"][0]
    formats = info.get("formats") or []
    duration = info.get("duration")

    audio_size = max(
        (
            _format_size(f, duration) or 0
            for f in formats
            if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        ),
        default=0,
    )

    # Keep the best (h264-preferred) video format per resolution. vcodec="none"
    # is audio-only; vcodec=None (unknown, e.g. Rule34Video) is still a video.
    best_per_height: dict[int, dict] = {}
    for fmt in formats:
        if fmt.get("vcodec") == "none":
            continue
        height = _format_height(fmt)
        if height is None:
            continue
        current = best_per_height.get(height)
        is_avc = str(fmt.get("vcodec", "")).startswith("avc1")
        current_is_avc = current is not None and str(current.get("vcodec", "")).startswith("avc1")
        if current is None or (is_avc and not current_is_avc):
            best_per_height[height] = fmt

    heights = sorted(best_per_height, reverse=True)

    def size_of(height: int) -> int | None:
        fmt = best_per_height[height]
        size = _format_size(fmt, duration)
        # HEAD a direct file when the size is unknown, but not an HLS manifest
        # (.m3u8 HEAD returns the tiny playlist size, not the media size).
        is_hls = ".m3u8" in str(fmt.get("url") or "") or "m3u8" in str(fmt.get("protocol") or "")
        if size is None and fmt.get("url") and not is_hls:
            size = _remote_size(fmt["url"], referer=url, proxy=proxy)
        if size is None:
            return None
        muxed = fmt.get("acodec") not in (None, "none")
        return size + (0 if muxed else audio_size)

    # Probe sizes in parallel — sequential HEAD requests made the menu slow.
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = dict(zip(heights, pool.map(size_of, heights), strict=True))

    # Include the resolution even when size is unknown (some HLS streams) —
    # the in-download size guard still enforces the limit.
    return [
        QualityOption(
            height=height,
            estimated_size=sizes[height],
            format_id=best_per_height[height].get("format_id"),
        )
        for height in heights
    ]


async def probe_quality_options(
    url: str, cookies_file: Path | None
) -> list[QualityOption]:
    """Async wrapper: every resolution the source offers, with sizes, best first.

    Returns [] when the site does not report per-format sizes (then the caller
    should fall back to a plain best-quality download).
    """
    try:
        return await asyncio.to_thread(
            _probe_quality_options_sync, url, _base_options(cookies_file, url),
            proxy_for(detect_platform(url)),
        )
    except YtdlpDownloadError:
        return []


def _fetch_description_sync(url: str, options: dict) -> str | None:
    info = _extract_info(options | {"skip_download": True}, url, download=False)
    if info is None:
        return None
    if info.get("entries"):
        info = info["entries"][0]
    title = (info.get("title") or "").strip()
    description = (info.get("description") or "").strip()
    # YouTube Shorts (and others) have a separate title — show it above the text.
    if title and description and title not in description:
        return f"{title}\n\n{description}"
    return description or title or None


async def fetch_description(url: str, cookies_file: Path | None) -> str | None:
    """Return the post's text/description (metadata only, no download)."""
    try:
        return await asyncio.to_thread(
            _fetch_description_sync, url, _base_options(cookies_file, url)
        )
    except YtdlpDownloadError:
        return None


def _probe_filesize(url: str, options: dict) -> int | None:
    """Estimate the size of the selected format combo without downloading.

    A probe failure is non-fatal: return None so the real download runs and
    surfaces a proper (convertible) error instead of crashing the handler.
    """
    try:
        info = _extract_info(options | {"skip_download": True}, url, download=False)
    except (YtdlpDownloadError, DownloadFailedError):
        return None
    if info is None:
        return None
    if info.get("entries"):
        info = info["entries"][0]
    total = 0
    for fmt in info.get("requested_formats") or [info]:
        size = fmt.get("filesize") or fmt.get("filesize_approx")
        if size is None:
            return None
        total += size
    return total


async def _download_best(
    url: str, options: dict, max_bytes: int, size_guard: dict
) -> tuple[Path, dict]:
    """Best-quality download that refuses to waste traffic on oversized files.

    Raises OversizedError as early as possible: from the metadata probe, from
    the in-download size guard, or from the final size check.
    """
    estimated = await asyncio.to_thread(_probe_filesize, url, options)
    if estimated is not None and estimated > max_bytes:
        logger.info("Estimated size %d bytes exceeds the %d limit", estimated, max_bytes)
        raise OversizedError(estimated, max_bytes)

    path, info = await asyncio.to_thread(_download_sync, url, options)
    size = path.stat().st_size
    if size > max_bytes:
        # The metadata gave no (reliable) size — detected only after the fact.
        raise OversizedError(size, max_bytes)
    return path, info


async def _download_capped(
    url: str, options: dict, capped_format: str, max_bytes: int, size_guard: dict
) -> tuple[Path, dict]:
    """Size-capped download: the last attempt, no further fallbacks."""
    try:
        path, info = await asyncio.to_thread(
            _download_sync, url, options | {"format": capped_format}
        )
    except YtdlpDownloadError as exc:
        # Either no fitting format exists or the size guard tripped mid-download.
        raise FileTooLargeError(size_guard.get("tripped") or 0, max_bytes) from exc
    size = path.stat().st_size
    if size > max_bytes:
        raise FileTooLargeError(size, max_bytes)
    return path, info


def _ffprobe_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read real width/height from the file so Telegram shows it undistorted.

    Some sites (HLS, Rule34Video) report no/wrong dimensions; without correct
    width+height Telegram renders the video as a square placeholder.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        width, height = result.stdout.strip().split("x")
        return int(width), int(height)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None, None


def photo_needs_document(path: Path, max_bytes: int) -> bool:
    """Whether an image must be sent as a document rather than an inline photo.

    Telegram rejects photos over the byte limit, or whose sides sum to more than
    10000 px, or whose aspect ratio is more extreme than 20:1
    (PHOTO_INVALID_DIMENSIONS) — common for rule34's huge/long images.
    """
    if path.stat().st_size > max_bytes:
        return True
    width, height = _ffprobe_dimensions(path)
    if not width or not height:
        return False
    return (width + height) > 10000 or max(width, height) > 20 * min(width, height)


def _build_media(path: Path, info: dict) -> Media:
    duration = info.get("duration")
    width, height = info.get("width"), info.get("height")
    if not width or not height:
        width, height = _ffprobe_dimensions(path)
    return Media(
        path=path,
        title=info.get("title") or "media",
        file_size=path.stat().st_size,
        duration=int(duration) if duration else None,
        width=width,
        height=height,
        description=info.get("description"),
    )


@asynccontextmanager
async def _temporary_download(
    url: str,
    options: dict,
    capped_format: str,
    tmp_prefix: str,
    max_bytes: int,
    cancel_event: threading.Event | None,
    size_guard: dict,
    capped: bool,
) -> AsyncIterator[Media]:
    tmpdir = tempfile.mkdtemp(prefix=tmp_prefix)
    try:
        options["outtmpl"] = str(Path(tmpdir) / "%(id)s.%(ext)s")
        try:
            if capped:
                path, info = await _download_capped(
                    url, options, capped_format, max_bytes, size_guard
                )
            else:
                path, info = await _download_best(url, options, max_bytes, size_guard)
        except DownloadCancelledError:
            raise
        except (OversizedError, FileTooLargeError, YoutubeDLError) as exc:
            # Catch the base YoutubeDLError so any yt-dlp failure (incl. raw
            # UnsupportedError/ExtractorError) is converted, never crashing the handler.
            # Exceptions raised inside the progress hook come back wrapped by yt-dlp,
            # so cancellation is detected by the event, not the exception type.
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError from exc
            if isinstance(exc, OversizedError | FileTooLargeError):
                raise
            if size_guard.get("tripped"):
                raise OversizedError(size_guard["tripped"], max_bytes) from exc
            logger.warning("yt-dlp failed for %s: %s", url, exc)
            raise DownloadFailedError(
                _user_message(exc), retry_via_proxy=_is_block_error(exc)
            ) from exc
        yield _build_media(path, info)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def download_video(
    url: str,
    cookies_file: Path | None,
    max_bytes: int = TELEGRAM_MAX_UPLOAD,
    progress: ProgressState | None = None,
    cancel_event: threading.Event | None = None,
    capped: bool = False,
    format_override: str | None = None,
    force_proxy: bool = False,
    ratelimit: int | None = None,
) -> AbstractAsyncContextManager[Media]:
    """Async context manager yielding the downloaded video; temp files are always removed.

    capped=False downloads the best quality and raises OversizedError as soon
    as the file is known not to fit; capped=True takes the size-capped ladder.
    format_override (e.g. a user-chosen resolution) implies a final capped attempt.
    force_proxy routes through the VLESS proxy even if direct looks reachable.
    ratelimit caps the download speed in bytes/s (None = unlimited).
    """
    size_guard = {"limit": max_bytes, "tripped": None}
    options = _base_options(cookies_file, url, force_proxy) | {
        "format": _VIDEO_FORMAT,
        "format_sort": _VIDEO_FORMAT_SORT,
        # Remux (no re-encode) into mp4 when codecs allow it, otherwise mkv.
        "merge_output_format": "mp4/mkv",
        "progress_hooks": [_progress_hook(progress, cancel_event, size_guard)],
    }
    if ratelimit:
        options["ratelimit"] = ratelimit
    return _temporary_download(
        url, options, format_override or _VIDEO_FORMAT_CAPPED, "tg-video-", max_bytes,
        cancel_event, size_guard, capped or format_override is not None,
    )


_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_ALBUM_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
_MUSIC_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}
MAX_ALBUM_ITEMS = 10  # Telegram media group limit


@dataclass(frozen=True, slots=True)
class AlbumMedia:
    """Photo/video items of a carousel post plus its music track, if any."""

    items: list[Path]  # photos and videos in post order
    music: Path | None
    description: str | None = None

    @property
    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.items)


_DESCRIPTION_KEYS = ("description", "content", "caption", "title")


def _album_description(tmpdir: str) -> str | None:
    """Pull the post caption from gallery-dl's per-file metadata JSON."""
    import json

    for json_path in Path(tmpdir).rglob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8", errors="ignore"))
        except (ValueError, OSError):
            continue
        for key in _DESCRIPTION_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _download_album_sync(
    url: str, tmpdir: str, cookies_file: Path | None, proxy: str | None = None
) -> AlbumMedia:
    """Fetch a photo/carousel post with gallery-dl (yt-dlp does not support them).

    Instagram carousels may mix photos and videos; TikTok photo posts carry a
    music track which gallery-dl downloads as the last file (tiktok.audio).
    """
    command = [
        sys.executable, "-m", "gallery_dl",
        "--quiet", "--write-metadata", "--directory", tmpdir, "-o", "tiktok.audio=true",
    ]
    if proxy:
        command += ["--proxy", proxy]
    if cookies_file is not None:
        command += ["--cookies", str(cookies_file)]
    command.append(url)
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        logger.info("gallery-dl exited with %d: %s", result.returncode, result.stderr.strip())

    # gallery-dl may nest output in per-site subdirectories — collect recursively.
    files = sorted(p for p in Path(tmpdir).rglob("*") if p.is_file())
    items = [
        p for p in files
        if p.suffix.lower() in _PHOTO_EXTENSIONS | _ALBUM_VIDEO_EXTENSIONS
    ]
    music = next((p for p in files if p.suffix.lower() in _MUSIC_EXTENSIONS), None)
    return AlbumMedia(
        items=items[:MAX_ALBUM_ITEMS], music=music, description=_album_description(tmpdir)
    )


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}
# rule34.xxx now gates its API behind a key; the post page still embeds the
# direct media URL, so we scrape it instead of using gallery-dl's API path.
_RULE34_MEDIA_RE = re.compile(
    r"https?://[a-z0-9.-]*rule34\.xxx/+images/[^\"' ]+\.(?:jpg|jpeg|png|gif|webm|mp4)",
    re.IGNORECASE,
)


def _scrape_rule34_sync(url: str, tmpdir: str, proxy: str | None = None) -> AlbumMedia:
    """Extract the direct media URL from a rule34.xxx post page and download it."""
    # rule34.xxx is behind Cloudflare: without a real browser TLS fingerprint the
    # request gets an anti-bot challenge page (no media). The challenge is served
    # intermittently even through the proxy, so retry, rotating the fingerprint.
    seen: list[str] = []
    for target in ("chrome", "safari", "chrome131", "chrome"):
        try:
            html = _curl_get(
                url, proxy=proxy, headers=_BROWSER_HEADERS,
                impersonate=target, timeout=30,
            ).text
        except Exception as exc:  # transient network/TLS — try the next fingerprint
            logger.info("rule34 fetch error (%s): %s", target, exc)
            html = ""
        for match in _RULE34_MEDIA_RE.findall(html):
            clean = match.replace("//images/", "/images/")
            if clean not in seen:
                seen.append(clean)
        if seen:
            break
        time.sleep(1.0)
    if not seen:
        raise DownloadFailedError("По этой ссылке не нашлось ни видео, ни фото.")

    # A video post embeds both a poster image and the clip — prefer the video.
    # Video lives on a dedicated CDN (nymp4.*); the image host (wimg.*) 403s for it.
    videos = sorted(
        (m for m in seen if m.lower().endswith((".mp4", ".webm"))),
        key=lambda m: "wimg." in m.lower(),
    )
    media_url = videos[0] if videos else seen[0]
    ext = Path(urlparse(media_url).path).suffix.lower() or ".jpg"
    dest = Path(tmpdir) / f"rule34{ext}"
    # The video CDN returns 403 without a Referer pointing back to the post page.
    response = _curl_get(
        media_url, proxy=proxy, headers={**_BROWSER_HEADERS, "Referer": url},
        timeout=120, stream=True,
    )
    with dest.open("wb") as fh:
        for chunk in response.iter_content():
            fh.write(chunk)
    return AlbumMedia(items=[dest], music=None)


@asynccontextmanager
async def download_album(
    url: str, cookies_file: Path | None, force_proxy: bool = False
) -> AsyncIterator[AlbumMedia]:
    """Async context manager yielding album media; temp files are always removed."""
    tmpdir = tempfile.mkdtemp(prefix="tg-album-")
    host = (urlparse(url).hostname or "").lower()
    _plat = detect_platform(url)
    proxy = forced_proxy(_plat) if force_proxy else proxy_for(_plat)
    try:
        try:
            if host.endswith("rule34.xxx"):
                album = await asyncio.to_thread(_scrape_rule34_sync, url, tmpdir, proxy)
            else:
                album = await asyncio.to_thread(
                    _download_album_sync, url, tmpdir, cookies_file, proxy
                )
        except subprocess.TimeoutExpired as exc:
            raise DownloadFailedError(
                "Не удалось скачать пост: превышено время ожидания."
            ) from exc
        except urllib.error.URLError as exc:
            raise DownloadFailedError("Не удалось скачать пост: сетевая ошибка.") from exc
        if not album.items:
            raise DownloadFailedError("По этой ссылке не нашлось ни видео, ни фото.")
        yield album
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def is_photo_post_url(url: str) -> bool:
    """Links that are image/carousel posts and should bypass the yt-dlp video path.

    Instagram /p/ posts can mix photos and videos (yt-dlp would grab only one
    video and silently drop the rest); TikTok /photo/ and rule34.xxx posts are
    not supported by yt-dlp at all.
    """
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path
    if host.endswith("rule34.xxx"):
        return True
    if host.endswith("instagram.com") and path.startswith("/p/"):
        return True
    return host.endswith("tiktok.com") and "/photo/" in path


# Spotify streams are DRM-protected and cannot be downloaded directly; spotdl
# reads the track metadata from Spotify and fetches the matching audio from
# YouTube. It lives in its own venv so its yt-dlp pin never clashes with ours.
_SPOTDL_BIN = os.getenv("SPOTDL_BIN", "/opt/spotdl/bin/spotdl")
# Spotify's API is geo-blocked in Russia, so spotdl's metadata + YouTube fetch
# are routed through an HTTP proxy (the VLESS node) when configured.
_SPOTDL_HTTP_PROXY = os.getenv("SPOTDL_HTTP_PROXY", "")


def _download_spotify_sync(url: str, tmpdir: str) -> Path:
    env = dict(os.environ)
    if _SPOTDL_HTTP_PROXY:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = _SPOTDL_HTTP_PROXY
    command = [
        _SPOTDL_BIN, "download", url,
        "--output", str(Path(tmpdir) / "{artists} - {title}.{output-ext}"),
        "--format", "mp3",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=600, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadFailedError("Spotify: превышено время ожидания.") from exc
    files = sorted(p for p in Path(tmpdir).rglob("*.mp3") if p.is_file())
    if not files:
        detail = (result.stderr or result.stdout or "").strip()
        logger.info("spotdl produced no file (rc=%d): %s", result.returncode, detail[:500])
        raise DownloadFailedError("Не удалось скачать трек из Spotify.")
    return max(files, key=lambda p: p.stat().st_size)


@asynccontextmanager
async def _spotify_download(url: str, max_bytes: int) -> AsyncIterator[Media]:
    tmpdir = tempfile.mkdtemp(prefix="tg-spotify-")
    try:
        path = await asyncio.to_thread(_download_spotify_sync, url, tmpdir)
        size = path.stat().st_size
        if size > max_bytes:
            raise FileTooLargeError(size, max_bytes)
        yield Media(
            path=path, title=path.stem, file_size=size,
            duration=None, width=None, height=None,
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def download_audio(
    url: str,
    cookies_file: Path | None,
    max_bytes: int = TELEGRAM_MAX_UPLOAD,
    progress: ProgressState | None = None,
    cancel_event: threading.Event | None = None,
    capped: bool = False,
    force_proxy: bool = False,
    ratelimit: int | None = None,
) -> AbstractAsyncContextManager[Media]:
    """Async context manager yielding the extracted audio; temp files are always removed."""
    if detect_platform(url) == "spotify":
        return _spotify_download(url, max_bytes)
    size_guard = {"limit": max_bytes, "tripped": None}
    options = _base_options(cookies_file, url, force_proxy) | {
        "format": _AUDIO_FORMAT,
        # "best" copies the source audio stream without re-encoding whenever possible.
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}],
        "progress_hooks": [_progress_hook(progress, cancel_event, size_guard)],
    }
    if ratelimit:
        options["ratelimit"] = ratelimit
    return _temporary_download(
        url, options, _AUDIO_FORMAT_CAPPED, "tg-audio-", max_bytes,
        cancel_event, size_guard, capped,
    )
