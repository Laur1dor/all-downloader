"""Media downloading built on yt-dlp.

Quality policy: always take the highest available resolution/FPS and, among
equal-quality formats, prefer H.264/AAC so videos play inline in every
Telegram client. Streams are merged/remuxed by ffmpeg but never re-encoded.
A size-capped retry happens only when the best-quality file cannot fit into
the Telegram Bot API upload limit at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtdlpDownloadError
from yt_dlp.utils import YoutubeDLError, download_range_func

from bot import instagram, youtube_posts
from bot.media_parts import MediaPreparationError, _run_ffmpeg, prepare_streaming_mp4
from bot.progress import PHASE_DOWNLOAD, PHASE_PREPARE, PHASE_RESOLVE, ProgressState
from bot.proxy import forced_proxy, proxy_for, proxy_ladder
from bot.rule34video import Rule34VideoIE
from bot.tiktok_live_photo import (
    LivePhotoError,
    PreparedLivePhoto,
    extract_embedded_motion,
    extract_live_photo_sources,
    prepare_live_photo,
)
from bot.urlkey import is_short_link

logger = logging.getLogger(__name__)

TELEGRAM_MAX_UPLOAD = 50 * 1024 * 1024  # Bot API hard limit for uploads
_DOWNLOAD_DISK_FLOOR_BYTES = 512 * 1024 * 1024
_RANGED_WORK_SECONDS = 30 * 60

_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
# TikTok publishes the post's real sound only inside its ready-made files. Its
# separate audio format is the track the post is attached to, so "best video +
# best audio" quietly built a different clip: a video-only HEVC stream with the
# music laid over it, in Matroska because HEVC and mp3 do not share an MP4.
# Telegram then played it without sound. Taking one already-muxed file gives the
# post's own audio, an MP4, and no merge step at all.
_VIDEO_FORMAT_MUXED = "b*[vcodec!=none][acodec!=none]/b/best"
_VIDEO_FORMAT_BY_PLATFORM = {"tiktok": _VIDEO_FORMAT_MUXED}


def video_format(platform: str) -> str:
    return _VIDEO_FORMAT_BY_PLATFORM.get(platform, _VIDEO_FORMAT)
# h264 first, because on YouTube the resolutions above 1080p exist ONLY in
# vp9/av1, and phones decode those in software: the video arrives as a frozen
# frame with sound. Ranking by size first really does pick 4K vp9 over 1080p
# h264 there, so this order stays the default.
_VIDEO_FORMAT_SORT = ["vcodec:h264", "res", "fps", "acodec:aac"]

# TikTok is the exception. It offers the *same* clip as a 576x1024 h264
# transcode next to its 720x1280 h265 original, so preferring the codec silently
# downgraded every download — which is why clips looked worse here than in the
# app. Its h265 is what the app itself plays, and there is no higher resolution
# hiding behind another codec, so resolution leads and h264 only breaks ties.
# Among the muxed files h264 comes first. The 1080p HEVC one is sharper on
# paper, but Telegram's players stumble on it, and the file that arrives is the
# one the person actually watches.
_VIDEO_FORMAT_SORT_BY_PLATFORM = {
    "tiktok": ["vcodec:h264", "res", "fps", "tbr"],
}


def video_format_sort(platform: str) -> list[str]:
    return _VIDEO_FORMAT_SORT_BY_PLATFORM.get(platform, _VIDEO_FORMAT_SORT)
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
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
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
PHOTO_CAPABLE_PLATFORMS = ("instagram", "tiktok", "rule34", "twitter", "pinterest")


# The prober watches for this file and re-verifies the TikTok pool at once
# instead of waiting out its interval. A node can die seconds after being
# picked, and the download that hit the dead one is the earliest evidence.
_REPROBE_TRIGGER = Path(os.getenv("TIKTOK_TRIGGER_FILE", "data/reprobe_tiktok"))


def request_tiktok_reprobe() -> None:
    try:
        _REPROBE_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        _REPROBE_TRIGGER.touch()
    except OSError as exc:  # a missing signal only costs a slower recovery
        logger.debug("Could not signal the prober: %s", exc)


class DownloadFailedError(Exception):
    """Download failure with a user-facing (Russian) message.

    retry_via_proxy marks errors (IP block, geo, rate-limit) that a different
    exit IP — i.e. the VLESS proxy — is likely to get past.
    """

    def __init__(self, message: str, *, retry_via_proxy: bool = False) -> None:
        super().__init__(message)
        self.retry_via_proxy = retry_via_proxy


class AlbumLimitError(DownloadFailedError):
    """A TikTok photo post exceeds the bounded album download budget."""


class AgeRestrictedError(DownloadFailedError):
    """Instagram said the post is age-restricted, so only the account can fetch it."""


class NotAVideoPostError(DownloadFailedError):
    """The link is a real post, but a carousel rather than a video.

    Its own class because the handler already knows what to do with a photo
    post, and the generic "could not download" message sends the reader looking
    for a fault where there is none.
    """


class ConfirmedVideoError(DownloadFailedError):
    """The resolver identified a video, so an album retry cannot help."""


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
    original_path: Path | None = None
    source_hdr: bool = False
    source_hdr_verified: bool = False
    original_matches_preview: bool = False


@dataclass(frozen=True, slots=True)
class SourceDocument:
    path: Path
    width: int
    height: int
    duration: float
    source_hdr: bool
    structure_modified: bool


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


# the-joi-database.com serves its HLS stream through the player's API endpoint
# (data-hls-source="/api/stream/<id>"); older pages embedded a raw .m3u8 URL in
# the JS. Either way the manifest URL is scraped out and handed to yt-dlp.
_M3U8_RE = re.compile(r'https?://[^"\' \\]+\.m3u8[^"\' \\]*')
_JOIDB_HLS_RE = re.compile(r"""data-hls-source=["']([^"']+)["']""")


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
    # The site occasionally answers 200 with a shell page missing the player.
    # A second fresh fetch is enough; avoid retrying the HLS/CDN ladder here.
    for attempt in range(2):
        html = _fetch_html(url, proxy=proxy)
        # Preferred: the player's HLS API endpoint (data-hls-source="/api/stream/…"),
        # resolved against the page URL since it is a site-relative path.
        match = _JOIDB_HLS_RE.search(html)
        if match:
            return urljoin(url, match.group(1).replace("&amp;", "&"))
        # Fallback: an embedded absolute .m3u8 URL (older page layout).
        match = _M3U8_RE.search(html)
        if match:
            return match.group(0).replace("&amp;", "&")
        if attempt == 0:
            time.sleep(0.5)
    raise DownloadFailedError("Не удалось найти видео на странице.")


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
    if not query or query.lower().startswith(("яндекс музыка", "yandex music")):
        raise DownloadFailedError("Не удалось определить трек Yandex Music.")
    return f"ytsearch1:{query}"



def _unwrap_login_redirect(url: str) -> str:
    """The video a TikTok login redirect is standing in front of.

    TikTok now answers some short links with /login?redirect_url=<the video>
    &enter_method=mandatory instead of the video - measured on several links,
    all of them clips that are region-blocked for this address. The expander
    took that as the expansion, so the bot went on to download the login page:
    no video in it ("this is a photo post"), then gallery-dl refusing an
    unsupported URL. The real address is in the query, and the ordinary exit
    ladder fetches it - through the configured router, every rung served a
    region-blocked clip that the direct route could not.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return url
    if not parsed.path.rstrip("/").endswith("/login"):
        return url
    from urllib.parse import parse_qs

    target = (parse_qs(parsed.query).get("redirect_url") or [""])[0]
    inner = urlparse(target)
    inner_host = (inner.hostname or "").lower()
    if inner.scheme in ("http", "https") and (
        inner_host == "tiktok.com" or inner_host.endswith(".tiktok.com")
    ):
        return target
    return url


async def expand_short_link(url: str) -> str:
    """Follow a per-share short link to the address that names the post.

    TikTok's vt./vm. links carry no id at all, so two links to one video look
    like two different posts to the cache. Following the redirect once is much
    cheaper than the download it saves. The original link is returned unchanged
    when the host does not hand out short links or the redirect cannot be read.
    """
    if not is_short_link(url):
        return url
    if (urlparse(url).hostname or "").lower().endswith("pin.it"):
        from curl_cffi.requests.exceptions import RequestException

        def _resolve_pin() -> str:
            from curl_cffi import requests as cffi_requests
            response = cffi_requests.get(url, impersonate="chrome124", timeout=12,
                                         allow_redirects=True)
            target = str(response.url)
            host = (urlparse(target).hostname or "").lower()
            return target if host == "pinterest.com" or host.endswith(".pinterest.com") else url
        try:
            return await asyncio.to_thread(_resolve_pin)
        except (OSError, RequestException) as exc:
            logger.info("Could not expand Pinterest short link %s: %s", url, exc)
            return url
    proxy = proxy_for(detect_platform(url))

    def _resolve() -> str:
        from curl_cffi import requests as cffi_requests

        # The newest-Chrome fingerprint is refused outright by TikTok's CDN, and
        # this used to use it: the link stayed short, and the failure surfaced
        # later as an SSL error against vt.tiktok.com. Exits are walked too,
        # because one of them may be the thing that is refusing.
        last_exc: Exception | None = None
        platform = detect_platform(url)
        deadline = time.monotonic() + 30
        for candidate in proxy_ladder(detect_platform(url)) or [proxy]:
            proxies = {"http": candidate, "https": candidate} if candidate else None
            # Two fingerprints, not five: a redirect barely depends on the
            # client, while a dead exit costs the full timeout on every attempt,
            # and the walk has to finish inside the patience of whoever sent the
            # link.
            for target in _TIKTOK_IMPERSONATE[:2]:
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    return url
                try:
                    current = url
                    for _ in range(4):
                        remaining = deadline - time.monotonic()
                        if remaining < 1:
                            return url
                        if platform == "tiktok":
                            parsed = urlparse(current)
                            host = (parsed.hostname or "").lower()
                            if (parsed.scheme != "https" or parsed.username or parsed.password
                                    or parsed.port not in (None, 443)
                                    or not (host == "tiktok.com"
                                            or host.endswith(".tiktok.com"))):
                                break
                            from bot.urlguard import ensure_public_url
                            ensure_public_url(current)
                        response = cffi_requests.head(
                            current, impersonate=target,
                            timeout=min(_TIKTOK_HEAD_TIMEOUT, remaining), proxies=proxies,
                            allow_redirects=platform != "tiktok",
                        )
                        if platform != "tiktok":
                            current = str(response.url or url)
                            break
                        if response.status_code not in (301, 302, 303, 307, 308):
                            break
                        location = response.headers.get("Location")
                        if not location:
                            break
                        current = _unwrap_login_redirect(urljoin(current, location))
                        if re.search(r"/(?:video|photo)/\d+(?:/|$)", urlparse(current).path):
                            break
                    else:
                        continue
                except Exception as exc:
                    last_exc = exc
                    continue
                resolved = _unwrap_login_redirect(current)
                if platform == "tiktok":
                    parsed = urlparse(resolved)
                    host = (parsed.hostname or "").lower()
                    # A redirect to a landing page or unrelated site is not a
                    # resolved post. Keep walking exits without losing TikTok
                    # routing, validation and its anonymous resolver fallback.
                    if (parsed.scheme != "https"
                            or parsed.username or parsed.password or parsed.port not in (None, 443)
                            or not (host == "tiktok.com" or host.endswith(".tiktok.com"))
                            or not re.search(r"/(?:video|photo)/\d+(?:/|$)", parsed.path)):
                        continue
                    ensure_public_url(resolved)
                if resolved != url:
                    return resolved
        if last_exc is not None:
            raise last_exc
        return url

    try:
        return await asyncio.to_thread(_resolve)
    except Exception as exc:
        logger.info("Could not expand the short link %s: %s", url, exc)
        return url


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


# Sites that block by TLS fingerprint (PornHub/Cloudflare) need browser
# impersonation, which requires the optional curl_cffi backend. Rule34Video is
# deliberately NOT here: it routes through WARP (routing.toml), and DDoS-Guard
# 504s a chrome-impersonated fingerprint arriving from a Cloudflare WARP IP —
# the plain TLS stack passes, so impersonation must stay off for it.
_IMPERSONATE_HOSTS = ("pornhub.com",)


def _impersonate_target():
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return ImpersonateTarget("chrome")
    except Exception:
        return None


# yt-dlp treats `cookiefile` as read-write and saves the jar back when it is
# done. That is fine for a file it owns and wrong for this one: it is shared
# with gallery-dl, with the embed reader and with the login that produces it,
# and it holds the Instagram session - the single value here that no code can
# regenerate without a browser and, sometimes, a person.
#
# Measured 18 Sep 2026: one yt-dlp run against a jar holding eight Instagram
# cookies left seven behind, and sessionid was the one it dropped. The same day,
# the live jar lost its session between two timestamps with no login in between,
# and the bot then reported Instagram as signed out - which it was, by its own
# hand. gallery-dl does the same on an extraction that succeeds; it only looked
# innocent because the first jar it was measured against made it fail early.
#
# So both get a copy. Whatever they write back dies with the temporary file,
# and the jar has exactly one author: the login that produces it.
_JAR_COPIES: dict[str, str] = {}


def _readonly_jar(cookies_file: Path | None) -> str | None:
    """A throwaway copy of the jar, so a tool that rewrites it cannot."""
    if cookies_file is None:
        return None
    try:
        source = cookies_file.read_bytes()
    except OSError as exc:
        logger.warning("Could not read the cookie jar: %s", exc)
        return None
    # Keyed by content, so a jar that has not changed is copied once rather than
    # once per download, and a refreshed one is picked up on its next use.
    key = hashlib.sha256(source).hexdigest()
    cached = _JAR_COPIES.get(key)
    if cached and os.path.exists(cached):
        return cached
    handle, copy_path = tempfile.mkstemp(prefix="tg-jar-", suffix=".txt")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(source)
    except OSError as exc:
        logger.warning("Could not stage the cookie jar: %s", exc)
        return str(cookies_file)
    for stale_key, stale in list(_JAR_COPIES.items()):
        if stale != copy_path:
            with suppress(OSError):
                os.unlink(stale)
            _JAR_COPIES.pop(stale_key, None)
    _JAR_COPIES[key] = copy_path
    return copy_path


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
        staged = _readonly_jar(cookies_file)
        if staged is not None:
            options["cookiefile"] = staged

    host = (urlparse(url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in _IMPERSONATE_HOSTS):
        target = _impersonate_target()
        if target is not None:
            options["impersonate"] = target
    if any(host == h or host.endswith("." + h) for h in _NOCHECKCERT_HOSTS):
        options["nocheckcertificate"] = True
    # Yandex Music resolves to a YouTube search rather than an HTTP URL.
    # Its search result and media must keep the configured YouTube route.
    _plat = "youtube" if re.match(r"^ytsearch\d*:", url) else (
        detect_platform(url) if url else ""
    )
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
        if size_guard is not None and time.monotonic() > size_guard.get("deadline", float("inf")):
            size_guard["deadline_hit"] = True
            raise DownloadFailedError("Превышено время загрузки видео (30 минут).")
        if data.get("status") != "downloading":
            return
        if size_guard is not None:
            now = time.monotonic()
            if now - size_guard.get("disk_checked_at", 0) >= 2:
                size_guard["disk_checked_at"] = now
                if shutil.disk_usage(tempfile.gettempdir()).free < _DOWNLOAD_DISK_FLOOR_BYTES:
                    size_guard["disk_low"] = True
                    raise DownloadFailedError("Недостаточно свободного места для загрузки.")
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
    # TikTok answers a request that arrived through a momentarily unhappy exit
    # with a page that carries no data block, or with a truncated body. Both
    # read like a broken extractor but clear on the next try, and without them
    # here roughly one download in five failed for the user while every node in
    # the pool tested healthy.
    "unable to extract universal data",
    "unexpected response from webpage",
    "error reading response",
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
                if detect_platform(url) == "rule34video":
                    ydl.add_info_extractor(Rule34VideoIE())
                info = ydl.extract_info(url, download=download)
            # A soft failure returns None instead of raising, and that path used
            # to give up at once while the raising one retried three times. Same
            # cause, same cure: through a pool exit it is usually one bad
            # response, and the next attempt succeeds.
            if info is None and attempt + 1 < attempts:
                ladder = proxy_ladder(detect_platform(url))
                if len(ladder) > 1:
                    nxt = ladder[(attempt + 1) % len(ladder)]
                    if nxt:
                        options = dict(options, proxy=nxt)
                logger.info("Extractor returned nothing (retry %d) for %s",
                            attempt + 1, url)
                time.sleep(1.5)
                continue
            return info
        except YtdlpDownloadError as exc:
            last_exc = exc
            transient = any(m in str(exc).lower() for m in _TRANSIENT_MARKERS)
            if attempt + 1 < attempts and transient:
                # Move to the next exit before trying again. Most of these
                # failures are the site refusing one address, and repeating the
                # request through that same address is guaranteed to fail again.
                ladder = proxy_ladder(detect_platform(url))
                if len(ladder) > 1:
                    nxt = ladder[(attempt + 1) % len(ladder)]
                    if nxt and options.get("proxy") != nxt:
                        options = dict(options, proxy=nxt)
                        logger.info("Transient extract error (retry %d, next exit %s): %s",
                                    attempt + 1, nxt, exc)
                    else:
                        logger.info("Transient extract error (retry %d): %s", attempt + 1, exc)
                else:
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
                retry_via_proxy=_retry_via_proxy(exc, url),
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
# The 5xx/gateway set covers DDoS-Guard "under attack" 502/504s (rule34video),
# which block by IP reputation and often clear from a different exit pool.
_BLOCK_MARKERS = (
    "ip address is blocked", "403", "forbidden", "not available in your country",
    "geo restrict", "geo-restrict", "rate-limit", "too many requests",
    "http error 502", "http error 503", "http error 504",
    "bad gateway", "gateway timeout", "service unavailable",
    "unexpected_eof_while_reading",
)


def _is_block_error(exc: YtdlpDownloadError) -> bool:
    return any(marker in str(exc).lower() for marker in _BLOCK_MARKERS)


def _retry_via_proxy(exc: Exception, url: str) -> bool:
    """A Rule34Video CDN transport failure can clear through another exit.

    Its page can load through WARP while the media server times out or resets
    that same connection. These failures need the alternate exit just like an
    explicit IP block; invalid formats and removed videos still fail normally.
    """
    return _is_block_error(exc) or (
        detect_platform(url) == "rule34video"
        and any(marker in str(exc).lower() for marker in (
            "ssl", "tls", "timed out", "timeout", "connection reset",
            "connection aborted", "connection refused", "remote end closed",
            "incomplete read", "incompleteread",
        ))
    )


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
            return f"{self.height}p · размер неизвестен"
        if self.estimated_size < 1024 * 1024:
            return f"{self.height}p · <1 МБ"
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
            f"{option.format_id}+ba[ext=m4a]"
            f"/{option.format_id}+ba/{option.format_id}"
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


def _verify_tls(url: str) -> bool:
    """False only for the hosts known to serve a broken certificate."""
    host = (urlparse(url).hostname or "").lower()
    return not any(host == h or host.endswith("." + h) for h in _NOCHECKCERT_HOSTS)


def _remote_size(url: str, referer: str | None = None, proxy: str | None = None) -> int | None:
    """Content-Length of a direct media URL via curl_cffi (HEAD, then ranged GET)."""
    from curl_cffi import requests as cffi_requests

    headers = {"Referer": referer} if referer else {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    verify = _verify_tls(url)
    try:
        head = cffi_requests.head(
            url, impersonate="chrome", timeout=15, headers=headers, proxies=proxies,
            verify=verify,
        )
        length = head.headers.get("Content-Length")
        if length and str(length).isdigit():
            return int(length)
        ranged = cffi_requests.get(
            url, impersonate="chrome", timeout=15, stream=True, proxies=proxies,
            headers={**headers, "Range": "bytes=0-0"}, verify=verify,
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


# Up to this many segments are weighed individually, which makes the total exact
# rather than extrapolated. A typical stream is a few dozen ~10 s segments, so
# most sizes land on the byte; only long streams fall back to sampling.
_HLS_EXACT_SEGMENTS = 60
# Samples taken from a longer stream instead. More than a handful buys little:
# the error is dominated by how unlike the body the sampled parts are.
_HLS_SAMPLE_SEGMENTS = 8
_HLS_HEAD_WORKERS = 10
_EXTINF_RE = re.compile(r"#EXTINF:\s*([0-9.]+)")


def _hls_size(
    manifest_url: str, referer: str | None = None, proxy: str | None = None
) -> int | None:
    """True size of an HLS variant, measured from its own segments.

    A master playlist's BANDWIDTH (which yt-dlp reports as ``tbr``) is a
    declared peak, not the real average: the-joi-database overstates it by
    16-19x, so a bitrate x duration estimate promised ~2 GB for a 120 MB file.
    Instead take the playlist's own EXTINF total as the real duration, weigh a
    few segments spread across the stream, and scale bytes-per-second up.
    """
    from curl_cffi import requests as cffi_requests

    headers = {"Referer": referer} if referer else {}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        playlist = cffi_requests.get(
            manifest_url, impersonate="chrome", timeout=20,
            headers=headers, proxies=proxies, verify=_verify_tls(manifest_url),
        ).text
    except Exception:
        return None
    # A master playlist lists variants, not media — nothing to weigh here.
    if "#EXT-X-STREAM-INF" in playlist:
        return None
    # Byte-range segments share one file; Content-Length would be the whole file.
    if "#EXT-X-BYTERANGE" in playlist:
        return None

    durations = [float(d) for d in _EXTINF_RE.findall(playlist)]
    segments = [
        urljoin(manifest_url, line.strip())
        for line in playlist.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    total_duration = sum(durations)
    if not segments or total_duration <= 0:
        return None

    if len(segments) <= _HLS_EXACT_SEGMENTS:
        # Cheap enough to weigh every segment: the answer is then the real size,
        # not an extrapolation, which is what the quality menu should show.
        picks = list(range(len(segments)))
    else:
        # Spread the samples over the stream: the first segment is often an intro
        # encoded at a different bitrate than the body.
        count = min(_HLS_SAMPLE_SEGMENTS, len(segments))
        picks = sorted({
            min(round((i + 1) * len(segments) / (count + 1)), len(segments) - 1)
            for i in range(count)
        })

    with ThreadPoolExecutor(max_workers=min(_HLS_HEAD_WORKERS, len(picks))) as pool:
        sizes = list(pool.map(
            lambda i: _remote_size(segments[i], referer=referer, proxy=proxy), picks
        ))

    # Every segment weighed and none missing → this is the exact size.
    if len(picks) == len(segments) and all(sizes):
        return sum(sizes)

    sampled_bytes = sum(s for s in sizes if s)
    sampled_seconds = sum(
        durations[i] for i, s in zip(picks, sizes, strict=True) if s and i < len(durations)
    )
    if not sampled_bytes or sampled_seconds <= 0:
        return None
    return int(sampled_bytes / sampled_seconds * total_duration)


_QUALITY_SIZE_POOL = ThreadPoolExecutor(max_workers=8)
_QUALITY_SIZE_PROBE_SECONDS = 4


def _probe_quality_options_sync(
    url: str, options: dict, proxy: str | None = None
) -> list[QualityOption]:
    """List all available resolutions with their estimated sizes, best first."""
    # Metadata is only for a menu. Avoid spending the retry budget of a real
    # download when a YouTube endpoint or proxy stops answering.
    probe_options = {k: v for k, v in options.items() if k != "format"} | {
        "socket_timeout": 8,
        "retries": 0,
        "extractor_retries": 0,
    }
    deadline = time.monotonic() + 25
    platform = detect_platform(url)
    if platform == "rule34video":
        try:
            info = _extract_info(
                probe_options | {"skip_download": True}, url, download=False, attempts=1,
            )
        except (YtdlpDownloadError, DownloadFailedError) as exc:
            alternate = forced_proxy(platform)
            retryable = _retry_via_proxy(exc, url) or getattr(exc, "retry_via_proxy", False)
            if not retryable or not alternate or alternate == probe_options.get("proxy"):
                raise
            info = _extract_info(
                probe_options | {"skip_download": True, "proxy": alternate},
                url, download=False, attempts=1,
            )
    else:
        info = _extract_info(probe_options | {"skip_download": True}, url, download=False)
    if info is None:
        return []
    if info.get("entries"):
        info = info["entries"][0]
    formats = info.get("formats") or []
    duration = info.get("duration")

    audio_formats = [
        f for f in formats
        if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
    ]
    # quality_selector first asks for AAC/m4a. Count only that audio track in
    # the size estimate; a large Opus stream can incorrectly lock every tier.
    m4a_formats = [f for f in audio_formats if f.get("ext") == "m4a"]
    audio_size = max(
        (_format_size(f, duration) or 0 for f in (m4a_formats or audio_formats)),
        default=0,
    )

    # Keep the best (H264-preferred) video format per resolution. A direct HTTP
    # MP4 wins over an HLS rendition of the same codec: the latter can be much
    # larger and requires ffmpeg to fetch its segments. vcodec=None (unknown,
    # e.g. Rule34Video) is still a video.
    best_per_height: dict[int, dict] = {}

    def preference(fmt: dict) -> tuple[bool, bool, float, float]:
        codec = str(fmt.get("vcodec") or "")
        protocol = str(fmt.get("protocol") or "")
        direct = protocol in ("https", "http")
        return (
            codec.startswith("avc1"), direct,
            float(fmt.get("fps") or 0), float(fmt.get("tbr") or 0),
        )

    for fmt in formats:
        if fmt.get("vcodec") == "none":
            continue
        height = _format_height(fmt)
        if height is None:
            continue
        current = best_per_height.get(height)
        if current is None or preference(fmt) > preference(current):
            best_per_height[height] = fmt

    heights = sorted(best_per_height, reverse=True)

    def size_of(height: int) -> int | None:
        fmt = best_per_height[height]
        size = _format_size(fmt, duration)
        if platform == "rule34video":
            # The menu is known from the page. CDN size requests can stall long
            # enough to discard every discovered resolution at the outer timeout.
            return size
        if time.monotonic() >= deadline:
            return size
        is_hls = ".m3u8" in str(fmt.get("url") or "") or "m3u8" in str(fmt.get("protocol") or "")
        if is_hls and fmt.get("url"):
            # For HLS the bitrate-derived estimate above is not just imprecise,
            # it is wrong by an order of magnitude on some sites — always prefer
            # a measurement of the actual segments when one can be taken.
            size = _hls_size(fmt["url"], referer=url, proxy=proxy) or size
        elif size is None and fmt.get("url"):
            # HEAD a direct file when the size is unknown. Never for a manifest:
            # HEAD on .m3u8 returns the tiny playlist size, not the media size.
            size = _remote_size(fmt["url"], referer=url, proxy=proxy)
        if size is None:
            return None
        muxed = fmt.get("acodec") not in (None, "none")
        return size + (0 if muxed else audio_size)

    # Optional CDN measurements must not discard resolutions already extracted
    # from the page. A shared pool bounds workers still finishing a timed-out
    # request, while metadata estimates remain usable for unfinished probes.
    sizes = {}
    futures = {}
    for height in heights:
        fmt = best_per_height[height]
        size = _format_size(fmt, duration)
        muxed = fmt.get("acodec") not in (None, "none")
        sizes[height] = None if size is None else size + (0 if muxed else audio_size)
        is_hls = ".m3u8" in str(fmt.get("url") or "") or "m3u8" in str(fmt.get("protocol") or "")
        if platform != "rule34video" and fmt.get("url") and (size is None or is_hls):
            futures[_QUALITY_SIZE_POOL.submit(size_of, height)] = height
    if futures:
        budget = max(0, min(_QUALITY_SIZE_PROBE_SECONDS, deadline - time.monotonic()))
        done, unfinished = wait(futures, timeout=budget)
        for future in done:
            try:
                measured = future.result()
            except Exception as exc:
                logger.debug("Optional quality size probe failed for %s: %s", url, exc)
                continue
            if measured is not None:
                sizes[futures[future]] = measured
        for future in unfinished:
            future.cancel()

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


_QUALITY_PROBE_SLOT = threading.Lock()
_QUALITY_PROBE_TIMEOUT = 35


def _limited_quality_probe(url: str, options: dict, proxy: str | None) -> list[QualityOption]:
    # asyncio can stop waiting for a thread, but cannot terminate it. At most
    # one timed-out yt-dlp metadata request may keep working in the background.
    if not _QUALITY_PROBE_SLOT.acquire(blocking=False):
        return []
    try:
        return _probe_quality_options_sync(url, options, proxy)
    finally:
        _QUALITY_PROBE_SLOT.release()


async def probe_quality_options(
    url: str, cookies_file: Path | None
) -> list[QualityOption]:
    """Async wrapper: every resolution the source offers, with sizes, best first.

    Resolutions remain selectable when the source does not report file sizes.
    Returns [] only when metadata is unavailable or no resolutions are found.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _limited_quality_probe, url, _base_options(cookies_file, url),
                proxy_for(detect_platform(url)),
            ), _QUALITY_PROBE_TIMEOUT,
        )
    except (TimeoutError, YtdlpDownloadError, DownloadFailedError):
        logger.info("Quality probe timed out or failed for %s; using default format", url)
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
        if detect_platform(url) == "tiktok":
            return await asyncio.to_thread(_fetch_tiktok_description_sync, url, cookies_file)
        return await asyncio.to_thread(
            _fetch_description_sync, url, _base_options(cookies_file, url)
        )
    except YtdlpDownloadError as exc:
        raise DownloadFailedError("Не удалось получить описание из источника.") from exc


def _fetch_tiktok_description_sync(url: str, cookies_file: Path | None) -> str | None:
    """Use TikTok's working media resolvers rather than the blocked yt-dlp API."""
    proxy = proxy_for("tiktok")
    try:
        item, session = _tiktok_item(
            url, proxy, _tiktok_cookies(cookies_file), photo_source=True,
        )
    except DownloadFailedError:
        data, session = _tikwm_data(url, proxy)
        try:
            return (data.get("title") or "").strip() or None
        finally:
            session.close()
    try:
        return (item.get("desc") or "").strip() or None
    finally:
        session.close()


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
    # A requested section is fetched with ffmpeg range requests.  The format
    # metadata describes the whole source and must not reject a small excerpt.
    estimated = None if options.get("download_ranges") else await asyncio.to_thread(
        _probe_filesize, url, options
    )
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
        # A network/extractor failure is not evidence that the file is too big.
        # Let the caller retry a blocked exit with the original yt-dlp error.
        if size_guard.get("tripped"):
            raise FileTooLargeError(size_guard["tripped"], max_bytes) from exc
        raise
    size = path.stat().st_size
    if size > max_bytes:
        raise FileTooLargeError(size, max_bytes)
    return path, info


def _ffprobe_stream(path: Path) -> dict:
    """First video stream as a dict, or {} when the file cannot be probed."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=codec_name,width,height,duration,sample_aspect_ratio:"
                "stream_tags=rotate:stream_side_data=rotation:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}
    streams = data.get("streams") or []
    stream = dict(streams[0]) if streams else {}
    if stream and not stream.get("duration"):
        stream["duration"] = (data.get("format") or {}).get("duration")
    return stream


def _stream_rotation(stream: dict) -> int:
    """Rotation in degrees the player will apply, 0 when there is none."""
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                return int(float(side_data["rotation"]))
            except (TypeError, ValueError):
                pass
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        return 0


def _ffprobe_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Displayed width/height, so Telegram shows the file undistorted.

    Coded dimensions alone are not what the viewer sees. Two things change them
    and both arrive from these sites: a rotation flag (a phone clip is stored
    landscape and flagged 90 degrees) and a non-square sample aspect ratio
    (anamorphic video, where the stored pixels are not square). Handing Telegram
    the coded numbers in either case stretches the picture, which is exactly how
    a rule34 video came out. Report what the player will actually draw.
    """
    stream = _ffprobe_stream(path)
    try:
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError):
        return None, None

    sar = str(stream.get("sample_aspect_ratio") or "")
    if ":" in sar:
        try:
            num, den = (int(x) for x in sar.split(":", 1))
            if num > 0 and den > 0:
                width = round(width * num / den)
        except (TypeError, ValueError):
            pass

    if abs(_stream_rotation(stream)) % 180 == 90:
        width, height = height, width
    return width, height


def video_metadata(path: Path) -> tuple[int | None, int | None, int | None]:
    """Displayed width, height and duration — what Telegram needs to render."""
    width, height = _ffprobe_dimensions(path)
    try:
        duration = int(float(_ffprobe_stream(path).get("duration") or 0)) or None
    except (TypeError, ValueError):
        duration = None
    return width, height, duration


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


def _ffprobe_duration(path: Path) -> int | None:
    """Length in seconds as the file itself reports it, or None."""
    try:
        return int(float(_ffprobe_stream(path).get("duration") or 0)) or None
    except (TypeError, ValueError):
        return None


def _build_media(path: Path, info: dict) -> Media:
    duration = info.get("duration")
    # The file itself is the authority: yt-dlp reports the coded size, which is
    # not what the player draws for rotated or anamorphic video, and conversion
    # to h264 bakes both of those in. Fall back to the metadata only if ffprobe
    # cannot read the file.
    width, height = _ffprobe_dimensions(path)
    if not width or not height:
        width, height = info.get("width"), info.get("height")
    actual_hdr = _source_hdr_state(info.get("original_path") or path)
    native_hdr = info.get("source_hdr")
    source_hdr = native_hdr is True or actual_hdr is True
    # A public SDR rendition can come from a hidden HDR upload. Its transfer
    # proves the rendition's color, not the original upload's color.
    source_hdr_verified = source_hdr or isinstance(native_hdr, bool)
    return Media(
        path=path,
        title=info.get("title") or "media",
        file_size=path.stat().st_size,
        duration=int(duration) if duration else None,
        width=width,
        height=height,
        description=info.get("description"),
        original_path=info.get("original_path"),
        source_hdr=source_hdr,
        source_hdr_verified=source_hdr_verified,
        original_matches_preview=bool(info.get("original_matches_preview")),
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
            if size_guard.get("deadline_hit"):
                raise DownloadFailedError("Превышено время загрузки видео (30 минут).") from exc
            if size_guard.get("disk_low"):
                raise DownloadFailedError("Недостаточно свободного места для загрузки.") from exc
            logger.warning("yt-dlp failed for %s: %s", url, exc)
            raise DownloadFailedError(
                _user_message(exc), retry_via_proxy=_retry_via_proxy(exc, url)
            ) from exc
        if detect_platform(url) == "twitter":
            try:
                path = await prepare_streaming_mp4(path, cancel_event)
            except MediaPreparationError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError from exc
                raise DownloadFailedError(str(exc)) from exc
            if path.stat().st_size > max_bytes:
                raise OversizedError(path.stat().st_size, max_bytes)
            info = dict(info, duration=_ffprobe_duration(path) or info.get("duration"))
        yield _build_media(path, info)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def _ranged_temp_bytes(directory: Path) -> int:
    """Count files being written by yt-dlp/ffmpeg, including partial streams."""
    total = 0
    for path in directory.rglob("*"):
        if path.suffix in (".json", ".log"):
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # yt-dlp atomically renames and removes partial files.
            continue
    return total


async def _stop_ranged_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    await process.wait()


@asynccontextmanager
async def _ranged_download(
    url: str, cookies_file: Path | None, max_bytes: int,
    progress: ProgressState | None, cancel_event: threading.Event | None,
    capped: bool, format_override: str | None, force_proxy: bool,
    ratelimit: int | None, time_range: tuple[int, int],
) -> AsyncIterator[Media]:
    """Isolate external ffmpeg so wall time, disk and cancellation can kill it."""
    directory = Path(tempfile.mkdtemp(prefix="tg-ranged-"))
    process = None
    try:
        environment = dict(os.environ, TMPDIR=str(directory), BOT_RANGED_WORKER="1")
        config = {
            "url": url, "cookies_file": str(cookies_file) if cookies_file else None,
            "max_bytes": max_bytes, "capped": capped,
            "format_override": format_override, "force_proxy": force_proxy,
            "ratelimit": ratelimit, "time_range": time_range,
            "directory": str(directory),
        }
        with (directory / "worker.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "bot.ranged_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=log, env=environment, start_new_session=os.name == "posix",
            )
            assert process.stdin is not None
            try:
                process.stdin.write(json.dumps(config).encode())
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise DownloadFailedError("Не удалось запустить загрузку фрагмента.") from exc
            deadline = time.monotonic() + _RANGED_WORK_SECONDS
            while process.returncode is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError
                if time.monotonic() >= deadline:
                    raise DownloadFailedError("Превышено время загрузки видео (30 минут).")
                written = _ranged_temp_bytes(directory)
                if progress is not None:
                    progress.downloaded = max(progress.downloaded, written)
                if written > max_bytes:
                    error = FileTooLargeError if capped else OversizedError
                    raise error(written, max_bytes)
                if shutil.disk_usage(directory).free < _DOWNLOAD_DISK_FLOOR_BYTES:
                    raise DownloadFailedError("Недостаточно свободного места для загрузки.")
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    pass
        result_file = directory / "result.json"
        try:
            result = (
                json.loads(result_file.read_text(encoding="utf-8"))
                if result_file.exists() else {}
            )
        except (OSError, ValueError):
            result = {}
        if process.returncode != 0 or "error" in result or "filename" not in result:
            kind = result.get("error_class")
            if kind == "OversizedError":
                raise OversizedError(int(result["size_bytes"]), max_bytes)
            if kind == "FileTooLargeError":
                raise FileTooLargeError(int(result["size_bytes"]), max_bytes)
            if kind == "DownloadCancelledError":
                raise DownloadCancelledError
            logger.warning(
                "Ranged worker failed for %s: %s", url,
                (directory / "worker.log").read_text(errors="replace")[-300:],
            )
            raise DownloadFailedError(
                str(result.get("error") or "Не удалось скачать фрагмент видео."),
                retry_via_proxy=bool(result.get("retry_via_proxy")),
            )
        media_file = directory / str(result["filename"])
        if not media_file.is_file():
            raise DownloadFailedError("Не удалось скачать фрагмент видео.")
        yield Media(
            path=media_file, title=str(result["title"]),
            file_size=media_file.stat().st_size,
            duration=result.get("duration"), width=result.get("width"),
            height=result.get("height"), description=result.get("description"),
        )
    finally:
        if process is not None:
            await _stop_ranged_process(process)
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


@asynccontextmanager
async def _rule34_video_download(
    url: str, cookies_file: Path | None, max_bytes: int,
    cancel_event: threading.Event | None, force_proxy: bool,
) -> AsyncIterator[Media]:
    """Use the post scraper for excerpts: yt-dlp cannot extract rule34.xxx."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError
    async with download_album(url, cookies_file, force_proxy) as album:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError
        if len(album.items) != 1 or album.items[0].suffix.lower() not in _ALBUM_VIDEO_EXTENSIONS:
            raise DownloadFailedError(
                "По этой ссылке фото. Интервал можно указать только для видео."
            )
        path = album.items[0]
        size = path.stat().st_size
        if size > max_bytes:
            raise OversizedError(size, max_bytes)
        width, height, duration = await asyncio.to_thread(video_metadata, path)
        yield Media(path, "rule34", size, duration, width, height, album.description)


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
    time_range: tuple[int, int] | None = None,
    original_only: bool = False,
) -> AbstractAsyncContextManager[Media]:
    """Async context manager yielding the downloaded video; temp files are always removed.

    capped=False downloads the best quality and raises OversizedError as soon
    as the file is known not to fit; capped=True takes the size-capped ladder.
    format_override (e.g. a user-chosen resolution) implies a final capped attempt.
    force_proxy routes through the VLESS proxy even if direct looks reachable.
    ratelimit caps the download speed in bytes/s (None = unlimited).
    original_only keeps TikTok source bytes for document delivery, including HDR.
    """
    platform = detect_platform(url)
    if platform == "rule34" and time_range is not None:
        return _rule34_video_download(
            url, cookies_file, max_bytes, cancel_event, force_proxy,
        )
    size_guard = {"limit": max_bytes, "tripped": None,
                  "deadline": time.monotonic() + 30 * 60}
    options = _base_options(cookies_file, url, force_proxy) | {
        "format": video_format(platform),
        "format_sort": video_format_sort(platform),
        # Remux (no re-encode) into mp4 when codecs allow it, otherwise mkv.
        # mp4 only for the platforms whose formats are already muxed: falling
        # back to Matroska there produced a file Telegram would not play.
        "merge_output_format": "mp4" if platform == "tiktok" else "mp4/mkv",
        "progress_hooks": [_progress_hook(progress, cancel_event, size_guard)],
    }
    if ratelimit:
        options["ratelimit"] = ratelimit
    if time_range is not None and platform == "youtube":
        if os.getenv("BOT_RANGED_WORKER") != "1":
            return _ranged_download(
                url, cookies_file, max_bytes, progress, cancel_event, capped,
                format_override, force_proxy, ratelimit, time_range,
            )
        options["download_ranges"] = download_range_func(None, [time_range])
        options["force_keyframes_at_cuts"] = True
        # yt-dlp uses ffmpeg for ranged inputs; its network reads bypass our
        # progress hook, so bound each stalled input read to 30 seconds.
        options["external_downloader_args"] = {
            "ffmpeg_i": ["-rw_timeout", "30000000"],
        }
        options["socket_timeout"] = 15
        options["retries"] = 1
        options["fragment_retries"] = 1
        options["extractor_retries"] = 1
    if platform == "tiktok":
        # yt-dlp cannot fetch a TikTok page at all here; see _tiktok_item.
        return _tiktok_download(
            url, cookies_file, max_bytes, progress, cancel_event, force_proxy, size_guard,
            original_only=original_only,
        )
    if platform == "instagram" and instagram.shortcode(url):
        # The embed page needs no session, so it is tried before the tools that
        # do. When it comes back with anything other than one video — a private
        # account, a carousel, a removed post — the context manager raises and
        # the caller falls through to yt-dlp exactly as before.
        return _instagram_first(
            url, max_bytes, progress, cancel_event, force_proxy, size_guard,
            # The fallback is handed over already built, rather than rebuilt
            # from a copy of this argument list: a copy is free to drift from
            # the original, and this one silently had, so every Instagram video
            # the embed could not serve died on a TypeError instead of falling
            # through to yt-dlp.
            lambda: _temporary_download(
                url, options, format_override or _VIDEO_FORMAT_CAPPED,
                "tg-video-", max_bytes, cancel_event, size_guard,
                capped or format_override is not None,
            ),
        )
    return _temporary_download(
        url, options, format_override or _VIDEO_FORMAT_CAPPED, "tg-video-", max_bytes,
        cancel_event, size_guard, capped or format_override is not None,
    )



# TikTok is fronted by Akamai, which answers the newest Chrome fingerprint with
# a 537-byte "Site Maintenance" page - and that is the one yt-dlp's extractor
# insists on: it asks for the best impersonation target available and ignores
# --impersonate entirely, so every TikTok download failed whichever exit it went
# through. Every other fingerprint is served the real page, so the page is
# fetched here and read directly.
# Ordered by what TikTok actually serves rather than by version: the Chrome
# targets are handed a WAF page ("SlardarWAF", ~1.5 KB) while Firefox and
# Safari get the real one. The first entry is what every exit is tried with,
# so it has to be the one most likely to work.
_TIKTOK_IMPERSONATE = ("firefox133", "safari17_0", "chrome131", "chrome124", "chrome116")
# A live exit answers TikTok in about a second; a dead free node accepts the
# connection and then says nothing, so the timeout is the whole cost of it.
_TIKTOK_TIMEOUT = 12
# Total budget for walking exits and fingerprints, before any video is fetched.
_TIKTOK_WALK_SECONDS = int(os.getenv("TIKTOK_WALK_SECONDS", "150"))
_TIKTOK_HEAD_TIMEOUT = 8
_TIKTOK_DATA_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S
)
_TIKTOK_REFERER = "https://www.tiktok.com/"


def _tiktok_cookies(cookies_file: Path | None) -> dict[str, str]:
    """TikTok cookies from the Netscape jar, if there are any.

    A signed-in request is answered with the full data block where an anonymous
    one gets a hollow copy, so the jar the rest of the bot already uses is worth
    carrying here too.
    """
    jar: dict[str, str] = {}
    if cookies_file is None:
        return jar
    try:
        for line in cookies_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.strip().split("	")
            if len(parts) >= 7 and "tiktok.com" in parts[0]:
                jar[parts[5]] = parts[6]
    except OSError:
        return {}
    return jar


def _tiktok_has_media(item: dict) -> bool:
    """Whether the post's data block actually carries something to download."""
    video = item.get("video") or {}
    if video.get("playAddr") or video.get("downloadAddr") or video.get("bitrateInfo"):
        return True
    images = (item.get("imagePost") or {}).get("images") or []
    return bool(images)


# TikTok refuses Russian addresses playback outright, and it does so *inside* a
# perfectly well-formed page: the data block is there, it just carries a status
# instead of the post. Read as a parse failure this looked like a bug in the
# extractor for as long as an exit happened to be alive, which is exactly the
# wrong place to go looking.
_TIKTOK_GEO_STATUS = 10204


def _tiktok_geo_block(detail: dict) -> str | None:
    """The message to show when TikTok refused the exit's country, else None."""
    if not isinstance(detail, dict) or detail.get("itemInfo"):
        return None
    status = detail.get("statusCode")
    message = str(detail.get("statusMsg") or "")
    if status != _TIKTOK_GEO_STATUS and "cross_border" not in message:
        return None
    return (
        "TikTok не отдаёт видео на этот адрес — региональная блокировка "
        f"({message or status}). Нужен живой выход: проверьте VPN в /vpn "
        "и переключатель в /control."
    )


def _remember_responsive(
    exit_proxy: str | None,
    target: str,
    responsive: list,
    attempts: list,
) -> None:
    """Queue the remaining fingerprints for an exit that answered.

    They go on the end, so every exit gets its first chance before any exit gets
    its second: the usual failure is a dead node, not a refused fingerprint.
    """
    if exit_proxy in responsive:
        return
    responsive.append(exit_proxy)
    attempts.extend(
        (exit_proxy, other) for other in _TIKTOK_IMPERSONATE if other != target
    )


def _tiktok_item(url: str, proxy: str | None, cookies: dict[str, str] | None = None,
                 *, photo_source: bool = False):
    """The post's data block, plus the session that fetched it.

    The session is handed back because the media host expects the same client: a
    fresh connection with a different fingerprint is refused just as the page
    would be.
    """
    from curl_cffi import requests as cffi_requests

    # Both axes matter and they fail differently: a refused fingerprint returns a
    # page with no data block, while a refused exit does not complete the TLS
    # handshake at all. Walking one without the other leaves half the failures
    # unrecoverable.
    exits = [proxy] if proxy in (None, "") else list(dict.fromkeys([
        proxy, *proxy_ladder("tiktok"),
    ]))
    # One fingerprint per exit on the first pass. The extra fingerprints are
    # worth spending only on an exit that answered at all, and that is what the
    # second pass over `responsive` does — otherwise a ladder of dead nodes
    # costs five timeouts each instead of one.
    responsive: list[str | None] = []
    attempts = [(e, _TIKTOK_IMPERSONATE[0]) for e in exits]
    last_reason = "no response"
    geo_reason: str | None = None
    # Eight exits, and four more fingerprints for each one that answers, is up to
    # forty requests; at twelve seconds apiece that is eight minutes spent before
    # a single byte of video is fetched, with nothing in the chat but a progress
    # bar. The walk gets a budget: when it is gone, the attempt has failed and
    # saying so is better than continuing to spend someone's wait.
    walk_began = time.monotonic()
    while attempts:
        if time.monotonic() - walk_began > (15 if photo_source else _TIKTOK_WALK_SECONDS):
            last_reason = f"{last_reason} (перебор выходов занял слишком долго)"
            break
        exit_proxy, target = attempts.pop(0)
        proxies = {"http": exit_proxy, "https": exit_proxy} if exit_proxy else None
        session = cffi_requests.Session(
            impersonate=target, proxies=proxies, timeout=_TIKTOK_TIMEOUT
        )
        if cookies:
            session.cookies.update(cookies)
        try:
            page = session.get(url, headers={"Referer": _TIKTOK_REFERER}).text
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            session.close()
            continue
        _remember_responsive(exit_proxy, target, responsive, attempts)
        found = _TIKTOK_DATA_RE.search(page)
        if not found:
            # The refusal page carries no data block, and another fingerprint may
            # be let through, so this is worth retrying rather than reporting.
            last_reason = f"no data block ({len(page)} bytes)"
            session.close()
            continue
        try:
            scope = json.loads(found.group(1))["__DEFAULT_SCOPE__"]
        except (KeyError, ValueError) as exc:
            last_reason = f"unexpected data block: {exc}"
            session.close()
            continue
        detail_key = "webapp.video-detail"
        if photo_source and detail_key not in scope and "webapp.photo-detail" in scope:
            detail_key = "webapp.photo-detail"
        if detail_key not in scope:
            # Measured on a /photo/ post: the page is served whole and simply
            # carries no video node, while a refusal carries the node with a
            # status inside it. So this is a carousel, and the photo path is
            # the one that can fetch it - walking more exits cannot help.
            session.close()
            raise NotAVideoPostError(
                "Это фото-пост, видео в нём нет."
            )
        detail = scope[detail_key]
        blocked = _tiktok_geo_block(detail)
        if blocked:
            # The refusal is about where the request left from, not how it
            # looked, so no other fingerprint on this exit will do better — but
            # another exit sits in another country and may well. Drop this
            # exit's remaining attempts and keep walking; the message is only
            # shown if every exit comes back the same way.
            geo_reason = blocked
            attempts[:] = [a for a in attempts if a[0] != exit_proxy]
            last_reason = "region block"
            session.close()
            continue
        try:
            item = detail["itemInfo"]["itemStruct"]
        except (KeyError, TypeError) as exc:
            last_reason = f"unexpected data block: {exc}"
            session.close()
            continue
        # The block can come back present but hollow — no media, no caption —
        # when TikTok decides the caller is not a browser. That is a refusal
        # wearing the shape of an answer, and reading it as "this post has no
        # video" told the user something false. Another fingerprint or exit
        # usually gets the real payload.
        if not _tiktok_has_media(item):
            last_reason = "data block carried no media"
            session.close()
            continue
        return item, session
    if geo_reason is not None:
        # Every exit that answered was refused for its country: that is the real
        # failure, and saying "TikTok did not return the page" instead sends the
        # reader looking in the extractor for a problem in the routing.
        raise DownloadFailedError(geo_reason, retry_via_proxy=True)
    raise DownloadFailedError(
        f"TikTok не отдал страницу поста ({last_reason}).", retry_via_proxy=True
    )


_TIKWM_API = "https://www.tikwm.com/api/"
_TIKWM_MEDIA_SUFFIXES = (
    "tikwm.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
)


def _tikwm_media_url(raw: str) -> str:
    """Accept only public CDN URLs from the untrusted resolver response."""
    if not isinstance(raw, str) or not raw:
        raise DownloadFailedError("Резервный сервис не вернул адрес медиа.")
    candidate = urljoin("https://www.tikwm.com/", raw)
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or not valid_port or parsed.username or parsed.password
            or not any(host == suffix or host.endswith("." + suffix)
                       for suffix in _TIKWM_MEDIA_SUFFIXES)):
        raise DownloadFailedError("Резервный сервис вернул недопустимый адрес медиа.")
    return candidate


def _tikwm_data(url: str, proxy: str | None = None) -> tuple[dict, object]:
    """Anonymous fallback: send only the public post URL, never TikTok cookies."""
    from curl_cffi import requests as cffi_requests

    if detect_platform(url) != "tiktok":
        raise DownloadFailedError("Резервный сервис принимает только ссылки TikTok.")
    last_error: Exception | None = None
    # Independent exits avoid repeating the same dead primary. The entire
    # resolver walk is bounded; sessions remain fresh and cookie-free.
    exits = list(dict.fromkeys([None, proxy, *proxy_ladder("tiktok")]))
    deadline = time.monotonic() + 45
    for exit_proxy in exits:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        proxies = {"http": exit_proxy, "https": exit_proxy} if exit_proxy else None
        session = cffi_requests.Session(
            impersonate="firefox133", proxies=proxies, timeout=min(15, remaining),
        )
        # Only this fresh, anonymous provider session may inspect provider URLs.
        # The HDR probe additionally refuses cookies, even with this marker.
        session._tiktok_hdr_anonymous_resolver = True
        try:
            response = session.get(_TIKWM_API, params={"url": url, "hd": "1"})
            if response.status_code != 200:
                raise DownloadFailedError("Резервный сервис TikTok недоступен.")
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if (not isinstance(payload, dict) or payload.get("code") != 0
                    or not isinstance(data, dict)):
                raise DownloadFailedError("Резервный сервис не нашёл пост TikTok.")
            post_id = str(data.get("id") or "")
            if not post_id.isdigit():
                raise DownloadFailedError("Резервный сервис вернул неверный пост.")
            path_id = re.search(r"/(?:video|photo)/(\d+)", urlparse(url).path)
            if path_id and post_id != path_id.group(1):
                raise DownloadFailedError("Резервный сервис вернул другой пост.")
            return data, session
        except Exception as exc:
            last_error = exc
            session.close()
    raise DownloadFailedError("Резервный сервис TikTok недоступен.") from last_error


def _tikwm_video_item(data: dict) -> dict:
    """Retain both resolver renditions; HD is a quality choice, never HDR proof."""
    sources = []
    for url_key, size_key in (("play", "size"), ("hdplay", "hd_size")):
        raw = data.get(url_key)
        if not raw:
            continue
        url = _tikwm_media_url(raw)
        if any(url in source["urls"] for source in sources):
            continue
        sources.append({"urls": [url], "codec": "", "bitrate": 0,
                        "width": None, "height": None, "size": data.get(size_key)})
    if not sources:
        raise DownloadFailedError("Резервный сервис не вернул адрес видео.")
    return {"id": data["id"], "desc": data.get("title") or "",
            "video": {"_resolver_sources": sources, "duration": data.get("duration")}}


def _tiktok_variants(item: dict, *, original_only: bool = False) -> list[dict]:
    """Playable files for the post, best first.

    h264 leads: TikTok offers the same clip as h264 beside a larger h265, and
    Telegram's players stumble on the latter. Within a codec the higher bitrate
    wins. Every entry is a complete file - the separate audio track is the song
    the post uses, not its own sound.
    """
    video = item.get("video") or {}
    if sources := video.get("_resolver_sources"):
        # Resolver HD addresses lack trustworthy native codec/geometry tags.
        # Preview uses its playable rendition first; File prefers the HD one.
        return list(reversed(sources)) if original_only else list(sources)
    variants: list[dict] = []
    for entry in video.get("bitrateInfo") or []:
        play = entry.get("PlayAddr") or {}
        urls = play.get("UrlList") or []
        if not urls:
            continue
        variants.append({
            "urls": list(urls),
            "codec": str(entry.get("CodecType") or ""),
            "bitrate": int(entry.get("Bitrate") or 0),
            "width": int(play.get("Width") or 0) or None,
            "height": int(play.get("Height") or 0) or None,
            "size": int(play.get("DataSize") or 0) or None,
        })
    fallback = video.get("playAddr") or video.get("downloadAddr")
    if not variants and fallback:
        variants.append({
            "urls": [fallback], "codec": "", "bitrate": 0,
            "width": video.get("width"), "height": video.get("height"),
            "size": video.get("dataSize"),
        })
    if original_only:
        variants.sort(key=lambda v: (
            -(v["width"] or 0) * (v["height"] or 0), -v["bitrate"],
        ))
    else:
        variants.sort(key=lambda v: (not v["codec"].startswith("h264"), -v["bitrate"]))
    return variants


class StalledTransferError(Exception):
    """A transfer that is technically alive but going nowhere."""


# curl_cffi turns a scalar timeout on a streaming request into a low-speed abort
# of one byte per second — a threshold a CDN trickling at 600 B/s never trips,
# and 600 B/s is exactly what the slow rows measured (0.75 MB in 1184s). So the
# transfer carries its own clock. The chunk is small because both clocks, and the
# cancel button with them, are only read between chunks: at a pathological
# 600 B/s a 32 KB chunk is 55 seconds, where a 256 KB one was seven minutes of a
# Отменить that did nothing.
_STREAM_CHUNK = 32 * 1024
_STREAM_STALL_SECONDS = int(os.getenv("STREAM_STALL_SECONDS", "45"))
# The budget follows the file, because a flat one would either cut a large
# download short or let a tiny one trickle for minutes. Twelve seconds per
# megabyte is about 85 KB/s — far below anything healthy, so only a genuinely
# broken exit reaches it.
_STREAM_FLOOR_SECONDS = 60
_STREAM_SECONDS_PER_MB = 12
_STREAM_CEILING_SECONDS = 600


def _stream_budget(size_bytes: int | None) -> float:
    """Seconds this transfer is allowed before it counts as stuck."""
    megabytes = (size_bytes or 0) / (1024 * 1024)
    return min(
        _STREAM_FLOOR_SECONDS + megabytes * _STREAM_SECONDS_PER_MB,
        _STREAM_CEILING_SECONDS,
    )


def _stream_to_file(
    session,
    media_url: str,
    destination: Path,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    size_guard: dict,
    declared_size: int | None,
    referer: str = _TIKTOK_REFERER,
    allow_redirects: bool = True,
) -> bool:
    """Stream one URL to disk, honouring the size limit and the cancel button.

    The referer travels with the request because media hosts check it: the CDN
    serves a file to the site it belongs to and refuses it to everybody else.
    """
    # A full HTTP body that fails media validation will be identical on every
    # exit. Tell the caller when Content-Length proved that the body is whole.
    size_guard["_last_complete_body"] = False
    if declared_size and declared_size > max_bytes:
        size_guard["tripped"] = declared_size
        raise OversizedError(declared_size, max_bytes)

    response = session.get(
        media_url, headers={"Referer": referer}, stream=True,
        allow_redirects=allow_redirects,
    )
    try:
        if response.status_code != 200:
            return False
        response_size = int(response.headers.get("Content-Length") or 0)
        total = response_size or declared_size
        if total and total > max_bytes:
            size_guard["tripped"] = total
            raise OversizedError(total, max_bytes)
        if progress is not None:
            progress.total = total or None
        written = 0
        budget = _stream_budget(total)
        began = time.monotonic()
        moved_at = began
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(_STREAM_CHUNK):
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError
                if now - began > budget:
                    raise StalledTransferError(
                        f"{written} bytes in {int(now - began)}s"
                    )
                if chunk:
                    moved_at = now
                elif now - moved_at > _STREAM_STALL_SECONDS:
                    raise StalledTransferError(f"no data for {int(now - moved_at)}s")
                handle.write(chunk)
                written += len(chunk)
                if written > max_bytes:
                    size_guard["tripped"] = written
                    raise OversizedError(written, max_bytes)
                if progress is not None:
                    progress.downloaded = written
        # A CDN can close a 200 response early without raising from iter_content.
        # Such an MP4 can still have a readable header but show a black frame.
        if total and written != total:
            raise StalledTransferError(f"incomplete media: {written}/{total} bytes")
        size_guard["_last_complete_body"] = bool(response_size and written == response_size)
        return written > 0
    finally:
        response.close()


def _tiktok_playable(path: Path) -> bool:
    """Reject error pages, broken MP4s and streams Telegram cannot play."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,pix_fmt,color_transfer,width,height,"
             "avg_frame_rate,r_frame_rate:"
             "stream_side_data:format=format_name,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if probe.returncode or not probe.stdout:
            logger.warning("TikTok media rejected: ffprobe failed")
            return False
        data = json.loads(probe.stdout)
        if not any(name in (data.get("format") or {}).get("format_name", "")
                   for name in ("mp4", "mov")):
            logger.warning("TikTok media rejected: unsupported container")
            return False
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None or video.get("codec_name") != "h264":
            logger.warning("TikTok media rejected: video codec is not H.264")
            return False
        duration = float((data.get("format") or {}).get("duration") or 0)
        if (
            not 0 < duration <= 4 * 3600
            or not (video.get("width", 0) > 0 and video.get("height", 0) > 0)
        ):
            logger.warning("TikTok media rejected: missing duration or dimensions")
            return False
        from bot.tiktok_hdr import hdr_state_from_stream

        if (
            video.get("pix_fmt", "yuv420p") not in ("yuv420p", "yuvj420p")
            or hdr_state_from_stream(video) is True
        ):
            logger.warning("TikTok media rejected: HDR or unsupported pixel format")
            return False
        if any(s.get("codec_type") == "audio" and s.get("codec_name") != "aac"
               for s in streams):
            logger.warning("TikTok media rejected: audio codec is not AAC")
            return False
        # Probe reads metadata, whereas this reads every packet. A truncated
        # mdat commonly passes ffprobe and fails here.
        decode = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "2",
             "-i", str(path),
             "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
            capture_output=True, timeout=_tiktok_validation_budget([video], duration), check=False,
        )
        if decode.returncode:
            stderr = decode.stderr.decode(errors="replace").lower()
            reason = (
                "invalid H.264 access unit" if "missing picture in access unit" in stderr
                else "decoder rejected packets"
            )
            logger.warning("TikTok media rejected: %s", reason)
        return decode.returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("TikTok media rejected: validation process failed")
        return False


def _repair_tiktok_sei(
    source: Path, max_bytes: int, cancel_event: threading.Event | None,
) -> Path | None:
    """Strip broken H.264 SEI metadata without re-encoding video or audio.

    Some complete TikTok MP4s contain malformed SEI access units that make
    strict decoders abort. This bitstream filter removes only SEI (NAL type 6);
    picture NAL units and audio packets are copied. The caller validates the
    entire result before any upload, and keeps the raw file for the file button.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:format=format_name", "-of", "json",
             str(source)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        data = json.loads(probe.stdout) if probe.returncode == 0 else {}
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if (video is None or video.get("codec_name") != "h264"
                or any(s.get("codec_type") == "audio" and s.get("codec_name") != "aac"
                       for s in streams)
                or not any(name in (data.get("format") or {}).get("format_name", "")
                           for name in ("mp4", "mov"))):
            return None
        if shutil.disk_usage(source.parent).free < (
            source.stat().st_size + _DOWNLOAD_DISK_FLOOR_BYTES
        ):
            logger.warning("TikTok SEI repair skipped: insufficient disk space")
            return None
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("TikTok SEI repair skipped: media probe failed")
        return None

    repaired = source.with_name(f"{source.stem}-compatible.mp4")
    repaired.unlink(missing_ok=True)
    completed = False
    try:
        _run_ffmpeg(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2",
             "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
             "-bsf:v", "filter_units=remove_types=6", "-movflags", "+faststart",
             str(repaired)],
            30, cancel_event,
        )
        if not repaired.is_file() or not 0 < repaired.stat().st_size <= max_bytes:
            logger.warning("TikTok SEI repair failed: output size invalid")
            return None
        completed = True
        return repaired
    except MediaPreparationError:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError from None
        logger.warning("TikTok SEI repair failed: ffmpeg copy step")
        return None
    finally:
        if not completed:
            repaired.unlink(missing_ok=True)


def _tiktok_conversion_budget(video: dict, duration: float) -> float:
    """Bound work by decoded pixels/frames instead of the clip's duration alone."""
    try:
        pixels = int(video.get("width") or 0) * int(video.get("height") or 0)
    except (TypeError, ValueError):
        pixels = 0
    fps = 30.0
    for field in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = float(Fraction(str(video.get(field) or "0")))
        except (ValueError, ZeroDivisionError):
            continue
        if candidate > 0:
            fps = candidate
            break
    depth_match = re.search(r"(\d+)(?:le|be)$", str(video.get("pix_fmt") or ""))
    bit_depth = int(depth_match.group(1)) if depth_match else 10
    # Scale the existing 12 s/s allowance from a 960x1920/30 fps/10-bit reference.
    # Full 4K/60 contains nine times as many pixels per second. Retain its
    # geometry, frame rate and CRF rather than timing out or lowering quality.
    work = max(1, pixels / (960 * 1920)) * max(1, fps / 30) * max(1, bit_depth / 10)
    return min(1800, max(120, duration * 12 * work))


def _tiktok_validation_budget(
    videos: list[dict], duration: float, *, floor: float = 90,
) -> float:
    return min(600, max(floor, sum(_tiktok_conversion_budget(v, duration) / 3 for v in videos)))


def _transcode_tiktok_compatible(
    source: Path, max_bytes: int, cancel_event: threading.Event | None,
) -> Path | None:
    """Convert a complete unsupported stream; retain its original bytes separately."""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,pix_fmt,color_transfer,width,height,"
             "avg_frame_rate,r_frame_rate:format=duration",
             "-of", "json", str(source)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        data = json.loads(probe.stdout) if probe.returncode == 0 else {}
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None or video.get("codec_name") not in ("h264", "hevc", "av1", "vp9", "mpeg4"):
            return None
        hdr = video.get("color_transfer") in ("smpte2084", "arib-std-b67")
        from bot.tiktok_hdr import hdr_state_from_stream

        if not hdr and hdr_state_from_stream(video) is True:
            # Dolby Vision without an HLG/PQ-compatible base needs a separate
            # color pipeline. Keep it for File and try a genuine SDR preview.
            return None
        if (
            video["codec_name"] == "h264" and not hdr
            and video.get("pix_fmt", "yuv420p") in ("yuv420p", "yuvj420p")
            and all(s.get("codec_name") == "aac" for s in streams if s.get("codec_type") == "audio")
        ):
            # Corrupt H.264 is handled by the lossless SEI repair, never concealed
            # by re-encoding a decoder's incomplete output.
            return None
        duration = float(data.get("format", {}).get("duration") or 0)
        if not 0 < duration <= 4 * 3600:
            return None
        available = shutil.disk_usage(source.parent).free - _DOWNLOAD_DISK_FLOOR_BYTES
        if available <= source.stat().st_size:
            logger.warning("TikTok conversion skipped: insufficient disk space")
            return None
        output_limit = min(max_bytes, available)
        # Ultrafast preserves CRF, resolution and frames, but costs more bytes.
        # The real 4K/60 sample used 7.5x its source size; reserve 10x headroom.
        preset = "ultrafast" if output_limit >= source.stat().st_size * 10 else "veryfast"
    except (OSError, ValueError, subprocess.SubprocessError):
        logger.warning("TikTok conversion skipped: media probe failed")
        return None

    converted = source.with_name(f"{source.stem}-telegram.mp4")
    converted.unlink(missing_ok=True)
    completed = False
    try:
        if any(stream.get("codec_type") == "audio" for stream in streams):
            # TikTok can attach an auxiliary AAC track with invalid packets.
            # Telegram plays the first original track; check it before spending
            # minutes converting HDR video, and leave auxiliary tracks in File.
            _run_ffmpeg(
                ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "2",
                 "-i", str(source), "-map", "0:a:0", "-f", "null", "-"],
                min(60, max(30, duration * 2)), cancel_event,
            )
        filters = []
        if hdr:
            # Convert HDR light into SDR before reducing to 8-bit: a pixel-format
            # conversion alone produces washed-out colors on Telegram clients.
            filters.extend([
                "zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
                "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv",
            ])
        filters.extend([
            "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'",
            "format=yuv420p",
        ])
        _run_ffmpeg(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", "-threads", "2",
             "-filter_threads", "1", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", ",".join(filters), "-c:v", "libx264", "-preset", preset,
             "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
             "-crf", "18", "-threads", "2", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-color_primaries", "bt709",
             "-color_trc", "bt709", "-colorspace", "bt709",
             "-fs", str(output_limit + 1), "-movflags", "+faststart", str(converted)],
            _tiktok_conversion_budget(video, duration), cancel_event,
        )
        if not converted.is_file() or not 0 < converted.stat().st_size <= output_limit:
            return None
        # -fs can terminate before the full timeline. Check duration as well as
        # codec compatibility so a size limit never becomes a successful clip.
        check = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(converted)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        actual = float(json.loads(check.stdout)["format"]["duration"])
        if check.returncode or abs(actual - duration) > 1:
            return None
        completed = True
        return converted
    except (
        OSError, ValueError, KeyError, subprocess.SubprocessError, MediaPreparationError,
    ) as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError from None
        logger.warning("TikTok compatibility conversion failed: %s", exc)
        return None
    finally:
        if not completed:
            converted.unlink(missing_ok=True)


def _source_hdr_state(path: Path) -> bool | None:
    """10-bit SDR alone is not HDR; require the HLG or PQ transfer function."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_transfer,pix_fmt:stream_side_data", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
        from bot.tiktok_hdr import hdr_state_from_stream

        return hdr_state_from_stream(streams[0]) if streams else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _source_hdr(path: Path) -> bool:
    return _source_hdr_state(path) is True


def _source_document_probe(path: Path) -> tuple[dict, dict, float]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,pix_fmt,width,height,color_transfer,color_primaries,"
             "color_space,avg_frame_rate,r_frame_rate:stream_side_data:format=duration,format_name",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        data = json.loads(result.stdout)
        video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
        duration = float(data["format"]["duration"])
        if (
            result.returncode or not 0 < duration <= 4 * 3600
            or not (video.get("width", 0) > 0 and video.get("height", 0) > 0)
        ):
            raise ValueError("invalid media metadata")
        return data, video, duration
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        raise DownloadFailedError("Не удалось проверить параметры исходного видео.") from exc


def _source_document_faststart(path: Path) -> bool:
    """Read only top-level MP4 box headers, never scan the large mdat payload."""
    try:
        import struct

        with path.open("rb") as stream:
            length = path.stat().st_size
            positions = {}
            while stream.tell() < length:
                position = stream.tell()
                header = stream.read(8)
                if len(header) != 8:
                    return False
                size, kind = struct.unpack(">I4s", header)
                minimum = 8
                if size == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return False
                    size = struct.unpack(">Q", extended)[0]
                    minimum = 16
                if size == 0:
                    size = length - position
                if size < minimum or position + size > length:
                    return False
                positions.setdefault(kind, position)
                stream.seek(position + size)
            return b"moov" in positions and b"mdat" in positions and (
                positions[b"moov"] < positions[b"mdat"]
            )
    except OSError:
        return False


def _source_document_decode(path: Path, cancel: threading.Event | None,
                            *, primary_only: bool = False) -> None:
    data, _, duration = _source_document_probe(path)
    videos = [s for s in data["streams"] if s.get("codec_type") == "video"]
    if primary_only:
        videos = videos[:1]
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-threads", "2",
               "-i", str(path), "-map", "0:v:0" if primary_only else "0:v",
               "-map", "0:a:0?" if primary_only else "0:a?", "-f", "null", "-"]
    _run_ffmpeg(command, _tiktok_validation_budget(videos, duration, floor=120), cancel)


def _source_document_audio_decode(
    path: Path, audio_index: int, cancel: threading.Event | None,
) -> None:
    _run_ffmpeg([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
        "-map", f"0:a:{audio_index}", "-vn", "-f", "null", "-",
    ], 60, cancel)


def _source_video_packet_hash(path: Path) -> str:
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-c", "copy", "-f", "hash", "-hash", "sha256", "-"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode or not result.stdout.strip().startswith("SHA256="):
        raise DownloadFailedError("Не удалось проверить сохранность исходного видеопотока.")
    return result.stdout.strip()


def _prepare_source_document(
    path: Path, max_bytes: int, cancel: threading.Event | None,
) -> SourceDocument:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelledError
    if path.stat().st_size > max_bytes:
        raise FileTooLargeError(path.stat().st_size, max_bytes)
    data, video, duration = _source_document_probe(path)
    from bot.tiktok_hdr import hdr_state_from_stream

    hdr = hdr_state_from_stream(video) is True
    output = path.with_name(f"{path.stem}-source.mp4")
    output.unlink(missing_ok=True)
    complete = False
    try:
        bad_audio = []
        audio = [stream for stream in data.get("streams", [])
                 if stream.get("codec_type") == "audio"]
        for index in range(len(audio)):
            try:
                _source_document_audio_decode(path, index, cancel)
            except MediaPreparationError:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelledError from None
                if index == 0:
                    raise
                bad_audio.append(index)
        if not bad_audio and _source_document_faststart(path):
            _source_document_decode(path, cancel)
            complete = True
            return SourceDocument(path, video["width"], video["height"], duration, hdr, False)
        if shutil.disk_usage(path.parent).free < path.stat().st_size + _DOWNLOAD_DISK_FLOOR_BYTES:
            raise DownloadFailedError("Недостаточно места для подготовки исходного файла.")
        command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(path)]
        command.extend(["-map", "0"])
        for index in bad_audio:
            command.extend(["-map", f"-0:a:{index}"])
        command.extend(["-map_metadata", "0", "-c", "copy"])
        if video.get("codec_name") == "hevc":
            command.extend(["-tag:v", "hvc1"])
        command.extend(["-movflags", "+faststart", str(output)])
        _run_ffmpeg(command, 60, cancel)
        if not output.is_file() or not 0 < output.stat().st_size <= max_bytes:
            raise DownloadFailedError("Подготовленный исходный файл превышает лимит отправки.")
        _, prepared, actual = _source_document_probe(output)
        properties = ("codec_name", "pix_fmt", "width", "height", "color_transfer",
                      "color_primaries", "color_space")
        if (
            any(video.get(key) != prepared.get(key) for key in properties)
            or hdr_state_from_stream(prepared) != hdr_state_from_stream(video)
            or abs(actual - duration) > 1
        ):
            raise DownloadFailedError("Подготовка изменила параметры исходного видео.")
        if _source_video_packet_hash(path) != _source_video_packet_hash(output):
            raise DownloadFailedError("Подготовка изменила исходный видеопоток.")
        _source_document_decode(output, cancel)
        if not _source_document_faststart(output):
            raise DownloadFailedError("Не удалось подготовить потоковый исходный MP4.")
        complete = True
        return SourceDocument(output, prepared["width"], prepared["height"], actual, hdr, True)
    except MediaPreparationError as exc:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelledError from None
        raise DownloadFailedError(
            "Исходный файл повреждён; сохранить видео и звук не удалось."
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise DownloadFailedError("Не удалось подготовить исходный видеофайл.") from exc
    finally:
        if not complete:
            output.unlink(missing_ok=True)


async def prepare_source_document(
    path: Path, max_bytes: int, cancel: threading.Event | None = None,
) -> SourceDocument:
    """Keep video/HDR packets; repair only packaging and broken auxiliary audio."""
    return await asyncio.to_thread(_prepare_source_document, path, max_bytes, cancel)


def _tiktok_original_valid(path: Path) -> bool:
    """Confirm a complete source is media without requiring Telegram preview codecs."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name:format=format_name,duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration") or 0)
        video = next((stream for stream in data.get("streams", [])
                      if stream.get("codec_type") == "video"), None)
        return (
            result.returncode == 0 and 0 < duration <= 4 * 3600
            and video is not None
            and video.get("codec_name") in ("h264", "hevc", "av1", "vp9", "mpeg4")
            and any(container in data.get("format", {}).get("format_name", "")
                    for container in ("mp4", "mov", "matroska", "webm"))
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _fetch_tiktok_media(
    item: dict, session, tmpdir: str, proxy: str | None, max_bytes: int,
    progress: ProgressState | None, cancel_event: threading.Event | None,
    size_guard: dict, *, resolver: bool, original_only: bool = False,
) -> tuple[Path, dict]:
    """Try complete variants with bounded, independent CDN exits."""
    from curl_cffi import requests as cffi_requests

    variants = _tiktok_variants(item, original_only=original_only)
    native_variants = _tiktok_variants(item, original_only=True)
    if not variants:
        raise DownloadFailedError("В этом посте TikTok нет видео.")
    from bot.tiktok_hdr import probe_tiktok_item_hdr

    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelledError
    if original_only:
        probe_tiktok_item_hdr(item, session)
        proven = set((item.get("video") or {}).get("_verified_hdr_urls") or [])
        known = {url for variant in variants for url in variant["urls"]}
        for url in proven - known:
            variants.append({"urls": [_tikwm_media_url(url)], "codec": "", "bitrate": 0,
                             "width": None, "height": None, "size": None})
        variants.sort(key=lambda variant: not proven.intersection(variant["urls"]))
        variants = [variant | {"urls": sorted(variant["urls"], key=lambda url: url not in proven)}
                    for variant in variants]
    destination = Path(tmpdir) / f"{item.get('id') or 'tiktok'}.mp4"
    last_reason = "no response"
    origin_proxy = getattr(session, "proxies", {}).get("https")
    routes = list(dict.fromkeys([origin_proxy, proxy, *proxy_ladder("tiktok"), None]))[:4]
    sessions = {origin_proxy: session}
    deadline = time.monotonic() + 180
    attempts = 0
    error_class = ConfirmedVideoError if resolver else DownloadFailedError
    try:
        for variant in variants:
            for media_url in variant["urls"]:
                for route in routes:
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelledError
                    if attempts >= 12 or time.monotonic() >= deadline:
                        raise error_class("Исчерпан лимит попыток загрузки TikTok.")
                    attempts += 1
                    if route not in sessions:
                        proxies = {"http": route, "https": route} if route else None
                        sessions[route] = cffi_requests.Session(
                            impersonate="firefox133", proxies=proxies, timeout=15,
                        )
                    media_session = sessions[route]
                    if progress is not None:
                        progress.phase = PHASE_DOWNLOAD
                    try:
                        written = _stream_to_file(
                            media_session, media_url, destination, max_bytes,
                            progress, cancel_event, size_guard, variant.get("size"),
                            referer="https://www.tikwm.com/" if resolver else _TIKTOK_REFERER,
                            allow_redirects=not resolver,
                        )
                    except (DownloadCancelledError, OversizedError):
                        raise
                    except (StalledTransferError, cffi_requests.exceptions.RequestException) as exc:
                        last_reason = f"{type(exc).__name__}: {exc}"
                        continue
                    selected = None
                    original = None
                    if written:
                        if original_only:
                            complete = size_guard.get("_last_complete_body") or (
                                variant.get("size")
                                and destination.stat().st_size == variant["size"]
                            )
                            if complete and _tiktok_original_valid(destination):
                                selected = destination
                        elif _tiktok_playable(destination):
                            selected = destination
                        else:
                            repaired = _repair_tiktok_sei(
                                destination, max_bytes, cancel_event,
                            )
                            if repaired is not None:
                                if _tiktok_playable(repaired):
                                    selected = repaired
                                    original = destination
                                    logger.info("TikTok media repaired by removing invalid SEI")
                                else:
                                    repaired.unlink(missing_ok=True)
                            if selected is None:
                                if progress is not None:
                                    progress.phase = PHASE_PREPARE
                                    progress.started_at = time.monotonic()
                                converted = _transcode_tiktok_compatible(
                                    destination, max_bytes, cancel_event,
                                )
                                if converted is not None:
                                    if _tiktok_playable(converted):
                                        selected = converted
                                        original = destination
                                        logger.info("TikTok media converted to H.264/AAC")
                                    else:
                                        converted.unlink(missing_ok=True)
                    if selected is not None:
                        video = item.get("video") or {}
                        actual_hdr = _source_hdr_state(original or selected)
                        native_hdr = True if actual_hdr is True else None
                        if not original_only and native_hdr is None:
                            native_hdr = probe_tiktok_item_hdr(item, session)
                        return selected, {
                            "id": item.get("id"),
                            "title": (item.get("desc") or "").strip() or "TikTok",
                            "description": (item.get("desc") or "").strip() or None,
                            "duration": video.get("duration"),
                            "width": variant.get("width") or video.get("width"),
                            "height": variant.get("height") or video.get("height"),
                            "original_path": original,
                            "original_matches_preview": variant == native_variants[0] and not (
                                native_hdr is True and actual_hdr is not True
                            ),
                            "source_hdr": native_hdr,
                        }
                    last_reason = (
                        "полученный файл не удалось подготовить для Telegram"
                        if written and size_guard.get("_last_complete_body")
                        else "сервер не передал видео полностью"
                    )
                    if written and size_guard.get("_last_complete_body"):
                        # The entire body was received. Repeating this URL on
                        # another exit would deliver the same damaged bytes.
                        break
        raise error_class(
            f"Не удалось скачать видео TikTok ({last_reason}).",
            retry_via_proxy=not resolver,
        )
    finally:
        for extra_session in sessions.values():
            if extra_session is not session:
                extra_session.close()


def _download_tiktok_sync(
    url: str,
    tmpdir: str,
    proxy: str | None,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    size_guard: dict,
    cookies: dict[str, str] | None = None,
    original_only: bool = False,
) -> tuple[Path, dict]:
    """Fetch directly, then use anonymous resolver for page or CDN failures."""
    direct_error: DownloadFailedError | None = None
    try:
        item, session = _tiktok_item(url, proxy, cookies)
    except DownloadFailedError as exc:
        direct_error = exc
    else:
        try:
            return _fetch_tiktok_media(
                item, session, tmpdir, proxy, max_bytes, progress,
                cancel_event, size_guard, resolver=False, original_only=original_only,
            )
        except (DownloadCancelledError, OversizedError):
            raise
        except DownloadFailedError as exc:
            direct_error = exc
        finally:
            session.close()

    # TikTok may serve a hollow page, or a working page with an unreachable
    # CDN. Both cases can still succeed through the anonymous resolver.
    resolver_session = None
    try:
        data, resolver_session = _tikwm_data(url, proxy)
        if is_photo_post_url(url) or data.get("images"):
            raise NotAVideoPostError("Это фото-пост, видео в нём нет.")
        item = _tikwm_video_item(data)
        return _fetch_tiktok_media(
            item, resolver_session, tmpdir, proxy, max_bytes, progress,
            cancel_event, size_guard, resolver=True, original_only=original_only,
        )
    except NotAVideoPostError:
        raise
    except ConfirmedVideoError:
        raise
    except (DownloadCancelledError, OversizedError):
        raise
    except Exception as exc:
        logger.warning("TikTok anonymous fallback failed: %s", exc)
        raise direct_error or DownloadFailedError("Не удалось скачать видео TikTok.") from exc
    finally:
        if resolver_session is not None:
            resolver_session.close()


@asynccontextmanager
async def _tiktok_download(
    url: str,
    cookies_file: Path | None,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    force_proxy: bool,
    size_guard: dict,
    original_only: bool = False,
) -> AsyncIterator[Media]:
    tmpdir = tempfile.mkdtemp(prefix="tg-tiktok-")
    proxy = forced_proxy("tiktok") if force_proxy else proxy_for("tiktok")
    cookies = _tiktok_cookies(cookies_file)
    try:
        path, info = await asyncio.to_thread(
            _download_tiktok_sync, url, tmpdir, proxy, max_bytes,
            progress, cancel_event, size_guard, cookies,
            original_only=original_only,
        )
        yield _build_media(path, info)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


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
    live_photos: dict[int, PreparedLivePhoto] | None = None

    @property
    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.items) + sum(
            pair.motion.stat().st_size for pair in (self.live_photos or {}).values()
        )


_DESCRIPTION_KEYS = ("description", "content", "caption", "title")


def _album_description(tmpdir: str) -> str | None:
    """Pull the post caption from gallery-dl's per-file metadata JSON."""
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




@asynccontextmanager
async def _instagram_first(
    url: str,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    force_proxy: bool,
    size_guard: dict,
    fallback,
) -> AsyncIterator[Media]:
    """Embed page first; whatever the caller normally does behind it."""
    try:
        async with _instagram_video(
            url, max_bytes, progress, cancel_event, force_proxy, size_guard
        ) as media:
            yield media
            return
    except (DownloadCancelledError, OversizedError, AgeRestrictedError):
        raise
    except Exception as exc:
        logger.info("Instagram embed did not serve %s (%s); using yt-dlp", url, exc)

    async with fallback() as media:
        yield media


_AGE_RESTRICTED_MESSAGE = (
    "Этот пост с возрастным ограничением: Instagram показывает его только "
    "залогиненным взрослым аккаунтам, а войти сейчас не получилось."
)


def _instagram_items_sync(
    url: str,
    tmpdir: str,
    proxy: str | None,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    size_guard: dict,
) -> tuple[list[Path], str | None]:
    """Every item of an Instagram post, fetched without an account.

    Returns ([], None) when the embed has nothing — a private account, a share
    link that wants a follow first, a removed post, or a change on Instagram's
    side. Which of those it is cannot be told apart here, and the difference
    matters to the caller only in that all of them go to the session tools: one
    of the posts this wrongly gave up on downloads from them without complaint.
    """
    post, session = instagram.read_post(url, proxy)
    if not post or session is None:
        if session is not None:
            session.close()
        if post.gate == instagram.AGE_GATE:
            raise AgeRestrictedError(_AGE_RESTRICTED_MESSAGE)
        return [], None

    paths: list[Path] = []
    try:
        for index, item in enumerate(post.items):
            suffix = ".mp4" if item.is_video else ".jpg"
            destination = Path(tmpdir) / f"{index:02d}{suffix}"
            written = _stream_to_file(
                session, item.url, destination, max_bytes,
                progress, cancel_event, size_guard, None,
                referer=instagram.REFERER,
            )
            if written:
                paths.append(destination)
    finally:
        session.close()
    return paths, post.caption


@asynccontextmanager
async def _instagram_video(
    url: str,
    max_bytes: int,
    progress: ProgressState | None,
    cancel_event: threading.Event | None,
    force_proxy: bool,
    size_guard: dict,
) -> AsyncIterator[Media]:
    """A single-video Instagram post, taken from the embed page."""
    tmpdir = tempfile.mkdtemp(prefix="tg-ig-")
    proxy = forced_proxy("instagram") if force_proxy else proxy_for("instagram")
    try:
        paths, caption = await asyncio.to_thread(
            _instagram_items_sync, url, tmpdir, proxy, max_bytes,
            progress, cancel_event, size_guard,
        )
        if len(paths) != 1 or paths[0].suffix != ".mp4":
            # Not a plain video post: either a carousel, which the album path
            # handles, or nothing at all. Either way yt-dlp gets its turn.
            raise DownloadFailedError("Instagram: пост не является одиночным видео.")
        # The embed carries no duration, and Telegram draws a scrubber from it:
        # without one the player shows a video of no length. The file is already
        # on disk, so ffprobe is the authority anyway.
        duration = await asyncio.to_thread(_ffprobe_duration, paths[0])
        yield _build_media(paths[0], {
            "title": "Instagram",
            "description": caption,
            "duration": duration,
        })
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


def _download_album_sync(
    url: str,
    tmpdir: str,
    cookies_file: Path | None,
    proxy: str | None = None,
    timeout: int | None = None,
) -> AlbumMedia:
    """Fetch a photo/carousel post with gallery-dl (yt-dlp does not support them).

    Instagram carousels may mix photos and videos; TikTok photo posts carry a
    music track which gallery-dl downloads as the last file (tiktok.audio).
    """
    command = [
        sys.executable, "-m", "gallery_dl",
        "--quiet", "--write-metadata", "--directory", tmpdir, "-o", "tiktok.audio=true",
    ]
    if is_photo_post_url(url) and detect_platform(url) == "tiktok":
        command += ["--range", "1-41", "--filesize-max", "20M"]
    if proxy:
        command += ["--proxy", proxy]
    if cookies_file is not None:
        # A copy here too. gallery-dl looked harmless when measured on an
        # extraction that failed - it left the file alone - but on one that
        # succeeds it writes the jar back like yt-dlp does, and a real download
        # then cost the jar a cookie. Measuring only the failing case is how
        # that got missed.
        staged = _readonly_jar(cookies_file)
        if staged is not None:
            command += ["--cookies", staged]
    command.append(url)
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout or 180
    )
    if result.returncode != 0:
        logger.info("gallery-dl exited with %d: %s", result.returncode, result.stderr.strip())

    # gallery-dl may nest output in per-site subdirectories — collect recursively.
    files = sorted(p for p in Path(tmpdir).rglob("*") if p.is_file())
    items = [
        p for p in files
        if p.suffix.lower() in _PHOTO_EXTENSIONS | _ALBUM_VIDEO_EXTENSIONS
    ]
    music = next((p for p in files if p.suffix.lower() in _MUSIC_EXTENSIONS), None)
    tiktok_photo = is_photo_post_url(url) and detect_platform(url) == "tiktok"
    limit = _TIKWM_ALBUM_MAX_ITEMS if tiktok_photo else MAX_ALBUM_ITEMS
    if len(items) > limit:
        raise AlbumLimitError("В посте слишком много файлов для одной загрузки.")
    if tiktok_photo and sum(p.stat().st_size for p in items) > _TIKWM_ALBUM_MAX_BYTES:
        raise AlbumLimitError("Фото-пост TikTok превышает лимит 80 МБ.")
    return AlbumMedia(items=items, music=music, description=_album_description(tmpdir))


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


_RULE34_API = "https://api.rule34.xxx/index.php"
_RULE34_POST_ID_RE = re.compile(r"[?&]id=(\d+)")


def _rule34_api_media(url: str, proxy: str | None) -> list[str]:
    """Media URLs for a rule34.xxx post through the site's own API.

    The HTML pages now answer with an interactive Cloudflare challenge that no
    TLS fingerprint gets past, so scraping them is a dead end. The API is the
    supported machine interface; it needs the account's `api_key` and `user_id`
    (Account -> Options on the site) in RULE34_API_KEY / RULE34_USER_ID.
    Returns [] when unconfigured so the caller can fall back and explain.
    """
    api_key = os.getenv("RULE34_API_KEY", "").strip()
    user_id = os.getenv("RULE34_USER_ID", "").strip()
    match = _RULE34_POST_ID_RE.search(url)
    if not (api_key and user_id and match):
        return []
    query = urlencode({
        "page": "dapi", "s": "post", "q": "index", "json": "1",
        "id": match.group(1), "api_key": api_key, "user_id": user_id,
    })
    try:
        response = _curl_get(
            f"{_RULE34_API}?{query}", proxy=proxy, headers=_BROWSER_HEADERS,
            impersonate="chrome", timeout=30,
        )
        posts = response.json()
    except Exception as exc:
        logger.info("rule34 API error: %s", exc)
        return []
    if not isinstance(posts, list) or not posts:
        return []
    post = posts[0]
    # file_url is the original; the sample is a downscaled copy kept as a
    # fallback for posts whose original is missing.
    return [u for u in (post.get("file_url"), post.get("sample_url")) if u]


def _scrape_rule34_sync(url: str, tmpdir: str, proxy: str | None = None) -> AlbumMedia:
    """Extract the direct media URL from a rule34.xxx post page and download it."""
    # rule34.xxx is behind Cloudflare: without a real browser TLS fingerprint the
    # request gets an anti-bot challenge page (no media). The challenge is served
    # intermittently even through the proxy, so retry, rotating the fingerprint.
    seen: list[str] = _rule34_api_media(url, proxy)
    for target in () if seen else ("chrome", "safari", "chrome131", "chrome"):
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
        # Distinguish "the post has nothing" from "the site refused to talk to
        # us": with the challenge in place the second is now the usual case, and
        # a retry through another exit will not help.
        if not (os.getenv("RULE34_API_KEY") and os.getenv("RULE34_USER_ID")):
            raise DownloadFailedError(
                "rule34.xxx закрыт проверкой Cloudflare — нужен ключ API "
                "(RULE34_API_KEY и RULE34_USER_ID)."
            )
        raise DownloadFailedError(
            "По этой ссылке не нашлось ни видео, ни фото.", retry_via_proxy=True
        )

    # A video post embeds both a poster image and the clip — prefer the video.
    # Video lives on a dedicated CDN (nymp4.*); the image host (wimg.*) 403s for it.
    videos = sorted(
        (m for m in seen if m.lower().endswith((".mp4", ".webm"))),
        key=lambda m: "wimg." in m.lower(),
    )
    media_url = videos[0] if videos else seen[0]
    from bot.urlguard import ensure_public_url
    ensure_public_url(media_url)
    ext = Path(urlparse(media_url).path).suffix.lower() or ".jpg"
    dest = Path(tmpdir) / f"rule34{ext}"
    # The media CDN returns 403 without a Referer pointing back to the post page,
    # and through a flaky proxy exit the body can arrive truncated or as an HTML
    # challenge page — writing that blindly gives Telegram a corrupt image that
    # renders with the wrong proportions. Validate the response and, on failure,
    # rotate the fingerprint (the pooled proxy also rotates its exit) and retry.
    last_reason = "no response"
    for target in ("chrome", "safari", "chrome131"):
        try:
            response = _curl_get(
                media_url, proxy=proxy,
                headers={**_BROWSER_HEADERS, "Referer": url},
                impersonate=target, timeout=120, stream=True,
            )
        except Exception as exc:  # transient network/TLS — try the next fingerprint
            last_reason = f"network error ({exc})"
            time.sleep(1.0)
            continue
        if response.status_code != 200:
            last_reason = f"HTTP {response.status_code}"
            response.close()
            time.sleep(1.0)
            continue
        expected = int(response.headers.get("Content-Length") or 0)
        with dest.open("wb") as fh:
            for chunk in response.iter_content():
                fh.write(chunk)
        if _rule34_media_ok(dest, expected):
            return AlbumMedia(items=[dest], music=None)
        last_reason = "truncated or invalid body"
        time.sleep(1.0)
    logger.info("rule34 media download failed: %s (%s)", media_url, last_reason)
    raise DownloadFailedError(
        "Не удалось скачать медиа rule34 (нестабильное соединение). Попробуйте ещё раз.",
        retry_via_proxy=True,
    )


def _rule34_media_ok(path: Path, expected_len: int) -> bool:
    """Reject a truncated download or an HTML challenge/error page saved as media.

    A corrupt image is the usual cause of rule34 pictures losing their aspect
    ratio in Telegram, so we drop anything that is too small, starts like HTML,
    or is shorter than the server-declared Content-Length.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < 1024:  # too small to be real media
        return False
    if expected_len and size < expected_len:  # stream dropped mid-transfer
        return False
    with path.open("rb") as fh:
        head = fh.read(64).lstrip().lower()
    return not (head.startswith(b"<!doctype") or head.startswith(b"<html") or head[:1] == b"<")


async def _instagram_browser_album(url: str, tmpdir: str) -> AlbumMedia | None:
    """The post as the browser sees it, when neither cheaper road served it.

    Last on purpose: it costs a page load and a Chromium where the embed costs
    one request. It earns that only in the state both other roads are shut at
    once - the embed answering with a null payload, and the account restricted
    so every API call comes back as the home page - which is exactly the state
    this was written in.
    """
    from bot import igbrowser

    post = await igbrowser.resolve(url)
    if post is None or not post.items:
        return None

    def _fetch() -> list[Path]:
        from curl_cffi import requests as cffi_requests

        paths: list[Path] = []
        session = cffi_requests.Session(impersonate="chrome131", timeout=60)
        try:
            for index, (media_url, is_video) in enumerate(post.items):
                destination = Path(tmpdir) / f"{index:02d}{'.mp4' if is_video else '.jpg'}"
                written = _stream_to_file(
                    session, media_url, destination, _ALBUM_ITEM_MAX_BYTES,
                    None, None, {}, None, referer=instagram.REFERER,
                )
                if written:
                    paths.append(destination)
        finally:
            session.close()
        return paths

    paths = await asyncio.to_thread(_fetch)
    if len(paths) != len(post.items):
        # All or nothing: half a carousel is not the post, and a partial album
        # looks like success.
        logger.info(
            "Browser gave %d item(s) for %s but only %d downloaded",
            len(post.items), url, len(paths),
        )
        return None
    return AlbumMedia(
        items=paths[:MAX_ALBUM_ITEMS], music=None, description=post.caption
    )


def _youtube_photo_album(url: str, tmpdir: str, proxy: str | None) -> AlbumMedia:
    """Download every community attachment in order, or reject the whole post."""
    from curl_cffi import requests as cffi_requests

    try:
        post = youtube_posts.resolve_post(url, proxy)
    except youtube_posts.YouTubePostError as exc:
        raise DownloadFailedError(str(exc)) from exc
    if not post.image_urls or len(post.image_urls) > 40:
        raise AlbumLimitError("В посте не найдено фото либо их больше 40.")
    session = cffi_requests.Session(impersonate="chrome131", timeout=30)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    paths = []
    total = 0
    deadline = time.monotonic() + 150
    try:
        for index, media_url in enumerate(post.image_urls):
            if time.monotonic() >= deadline:
                raise DownloadFailedError("Превышено время загрузки фото YouTube.")
            destination = Path(tmpdir) / f"youtube-{index:03d}"
            written = _stream_to_file(
                session, media_url, destination, 20 * 1024 * 1024,
                None, None, {}, None, referer="https://www.youtube.com/",
                allow_redirects=False,
            )
            extension = _tikwm_photo_extension(destination) if written else None
            if extension is None:
                raise DownloadFailedError("YouTube не отдал одно из фото поста целиком.")
            renamed = destination.with_suffix(extension)
            destination.rename(renamed)
            paths.append(renamed)
            total += renamed.stat().st_size
            if total > 80 * 1024 * 1024:
                raise AlbumLimitError("Фото-пост YouTube превышает лимит 80 МБ.")
    except DownloadFailedError:
        raise
    except Exception as exc:
        raise DownloadFailedError("Не удалось скачать все фото поста YouTube.") from exc
    finally:
        session.close()
    return AlbumMedia(paths, None, post.description)


@asynccontextmanager
async def download_album(
    url: str, cookies_file: Path | None, force_proxy: bool = False
) -> AsyncIterator[AlbumMedia]:
    """Async context manager yielding album media; temp files are always removed."""
    tmpdir = tempfile.mkdtemp(prefix="tg-album-")
    host = (urlparse(url).hostname or "").lower()
    _plat = detect_platform(url)
    proxy = forced_proxy(_plat) if force_proxy else proxy_for(_plat)
    # A dead exit makes gallery-dl come back empty, and "no video and no photos
    # here" is then a claim about the post that simply is not true. TikTok is
    # the platform where exits die under us, and it is the one with a ladder of
    # them, so the carousel gets the same walk the video path has.
    exits = _album_exits(_plat, proxy)
    try:
        if _plat == "tiktok":
            try:
                native = await asyncio.to_thread(
                    _tiktok_native_photo_album, url, tmpdir, proxy, _tiktok_cookies(cookies_file),
                )
            except LivePhotoError as exc:
                raise DownloadFailedError(str(exc)) from exc
            except (DownloadFailedError, NotAVideoPostError) as exc:
                logger.info("TikTok native photo metadata unavailable: %s", exc)
                native = None
            if native is not None:
                yield native
                return
            # Tikwm reports every slide. gallery-dl's ordinary result was
            # silently truncated to one Telegram group of ten images.
            try:
                album = await asyncio.to_thread(_tikwm_photo_album, url, tmpdir, proxy)
            except LivePhotoError as exc:
                raise DownloadFailedError(str(exc)) from exc
            except AlbumLimitError:
                raise
            except Exception as exc:  # noqa: BLE001 - fall back to gallery-dl
                logger.info("TikTok photo resolver unavailable: %s", exc)
                await asyncio.to_thread(_clear_directory, tmpdir)
            else:
                yield album
                return
        album = None
        last_error: DownloadFailedError | None = None
        # The browser has its own network in its own container, so walking the
        # exit ladder changes nothing for it. It used to run once per exit -
        # twenty seconds each, twice over, for the same answer.
        browser_tried = False
        for attempt, exit_proxy in enumerate(exits):
            if attempt:
                # Each attempt starts from an empty directory: a half-finished
                # download from a dying exit would otherwise be counted as the
                # post and sent as a truncated carousel.
                await asyncio.to_thread(_clear_directory, tmpdir)
            try:
                if youtube_posts.is_post_url(url):
                    try:
                        album = await asyncio.to_thread(
                            _youtube_photo_album, url, tmpdir, exit_proxy,
                        )
                    except DownloadFailedError as exc:
                        last_error = exc
                        continue
                elif host.endswith("rule34.xxx"):
                    album = await asyncio.to_thread(
                        _scrape_rule34_sync, url, tmpdir, exit_proxy
                    )
                elif _plat == "instagram" and instagram.shortcode(url):
                    # No session, so nothing here can expire. gallery-dl still
                    # runs when this comes back empty, which is what a private
                    # account looks like.
                    try:
                        paths, caption = await asyncio.to_thread(
                            _instagram_items_sync, url, tmpdir, exit_proxy,
                            _ALBUM_ITEM_MAX_BYTES, None, None, {},
                        )
                    except AgeRestrictedError as exc:
                        # Instagram said so itself, and nothing anonymous can
                        # serve it: straight to the roads that use the account.
                        # gallery-dl first - two seconds while the account's
                        # API is restricted, but the quick one once it is not -
                        # then the browser, which measured 4 of 4 at 12-13s.
                        age_error = exc
                        paths, caption = [], None
                        album = None
                        try:
                            album = await asyncio.to_thread(
                                _download_album_sync, url, tmpdir, cookies_file,
                                exit_proxy, None,
                            )
                        except DownloadFailedError:
                            album = None
                        if (album is None or not album.items) and not browser_tried:
                            browser_tried = True
                            await asyncio.to_thread(_clear_directory, tmpdir)
                            album = await _instagram_browser_album(url, tmpdir)
                        if album is None or not album.items:
                            # Walking the exit ladder cannot change an age gate.
                            last_error = age_error
                            break
                        break
                    if paths:
                        album = AlbumMedia(
                            items=paths[:MAX_ALBUM_ITEMS], music=None,
                            description=caption,
                        )
                    else:
                        try:
                            album = await asyncio.to_thread(
                                _download_album_sync, url, tmpdir, cookies_file,
                                exit_proxy,
                                _ALBUM_RETRY_TIMEOUT if attempt else None,
                            )
                        except DownloadFailedError:
                            album = None
                        if (album is None or not album.items) and not browser_tried:
                            # Both cheaper roads came back with nothing, which
                            # is the state the browser path exists for.
                            browser_tried = True
                            await asyncio.to_thread(_clear_directory, tmpdir)
                            album = await _instagram_browser_album(url, tmpdir)
                else:
                    album = await asyncio.to_thread(
                        _download_album_sync, url, tmpdir, cookies_file, exit_proxy,
                        _ALBUM_RETRY_TIMEOUT if attempt else None,
                    )
            except subprocess.TimeoutExpired as exc:
                last_error = DownloadFailedError(
                    "Не удалось скачать пост: превышено время ожидания."
                )
                last_error.__cause__ = exc
                continue
            except urllib.error.URLError as exc:
                last_error = DownloadFailedError("Не удалось скачать пост: сетевая ошибка.")
                last_error.__cause__ = exc
                continue
            # None is a real answer here: every Instagram road can come back
            # empty, and the browser returns None rather than an empty album.
            # Reading .items off it crashed instead of saying "not found".
            if album is not None and album.items:
                break
        if (album is None or not album.items) and _plat == "tiktok":
            await asyncio.to_thread(_clear_directory, tmpdir)
            try:
                album = await asyncio.to_thread(_tikwm_photo_album, url, tmpdir, proxy)
            except LivePhotoError as exc:
                raise DownloadFailedError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - gallery-dl's error is more useful
                logger.warning("TikTok photo fallback failed: %s", exc)
        if album is None or not album.items:
            if last_error is not None:
                raise last_error
            raise DownloadFailedError("По этой ссылке не нашлось ни видео, ни фото.")
        yield album
    finally:
        await asyncio.to_thread(shutil.rmtree, tmpdir, ignore_errors=True)


# Retries are given a shorter budget than the first try: the first is usually
# the only one, while a retry exists because an exit is already suspect.
_ALBUM_MAX_EXITS = 4
# Per item, not per post: an album is many files and the caller's ceiling is
# for one upload. Generous, because a carousel item that trips it is a bug
# in the reading, not a real post.
_ALBUM_ITEM_MAX_BYTES = 512 * 1024 * 1024
_ALBUM_RETRY_TIMEOUT = 60
_TIKWM_PHOTO_MAX_BYTES = 20 * 1024 * 1024
_TIKWM_ALBUM_MAX_ITEMS = 40
_TIKWM_ALBUM_MAX_BYTES = 80 * 1024 * 1024
_TIKWM_ALBUM_SECONDS = 150
_TIKWM_MUSIC_MAX_BYTES = 10 * 1024 * 1024
_TIKWM_MEDIA_PROXY = os.getenv("TIKWM_MEDIA_PROXY", "socks5h://warp:1080")
_TIKWM_CDN_XRAY_EXITS = (
    "socks5h://xray:2075", "socks5h://xray:2074", "socks5h://xray:2073",
)


def _tikwm_photo_extension(path: Path) -> str | None:
    """Decode one whole image before accepting a resolver-provided carousel."""
    with path.open("rb") as handle:
        prefix = handle.read(16)
    if prefix.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
    elif prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
    elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        extension = ".webp"
    else:
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return extension if result.returncode == 0 else None


def _tiktok_native_photo_album(
    url: str, tmpdir: str, proxy: str | None, cookies: dict[str, str],
) -> AlbumMedia | None:
    """Fetch explicitly paired native photo/motion assets in their source order."""
    item, session = _tiktok_item(url.replace("/photo/", "/video/", 1), proxy, cookies,
                                 photo_source=True)
    try:
        sources = extract_live_photo_sources(item)
        if not any(source.motion_urls for source in sources):
            return None
        if len(sources) > _TIKWM_ALBUM_MAX_ITEMS:
            raise LivePhotoError("В посте больше 40 фото; отправка ограничена.")
        paths = []
        pairs = {}
        total = 0
        deadline = time.monotonic() + _TIKWM_ALBUM_SECONDS

        def fetch(urls: tuple[str, ...], destination: Path, limit: int) -> None:
            for media_url in urls:
                if time.monotonic() >= deadline:
                    raise LivePhotoError("Превышено время загрузки Live Photo.")
                try:
                    if _stream_to_file(session, media_url, destination, limit,
                                       None, None, {}, None, allow_redirects=False):
                        return
                except (OSError, StalledTransferError):
                    continue
            raise LivePhotoError("TikTok не отдал один из файлов Live Photo.")

        for source in sources:
            photo = Path(tmpdir) / f"native-photo-{source.index}"
            fetch(source.photo_urls, photo, _TIKWM_PHOTO_MAX_BYTES)
            extension = _tikwm_photo_extension(photo)
            if extension is None:
                raise LivePhotoError("TikTok отдал повреждённое фото.")
            renamed = photo.with_suffix(extension)
            photo.rename(renamed)
            paths.append(renamed)
            total += renamed.stat().st_size
            if source.motion_urls:
                motion = Path(tmpdir) / f"native-motion-{source.index}.mp4"
                fetch(source.motion_urls, motion, _TIKWM_ALBUM_MAX_BYTES)
                total += motion.stat().st_size
                pairs[source.index] = prepare_live_photo(renamed, motion, Path(tmpdir))
            if total > _TIKWM_ALBUM_MAX_BYTES:
                raise LivePhotoError("Фото-пост TikTok превышает лимит 80 МБ.")
        return AlbumMedia(paths, None, item.get("desc") or None, pairs)
    except LivePhotoError:
        raise
    except Exception as exc:
        raise LivePhotoError("Не удалось получить полную пару фото и движения TikTok.") from exc
    finally:
        session.close()


def _tikwm_photo_album(url: str, tmpdir: str, proxy: str | None = None) -> AlbumMedia:
    from curl_cffi import requests as cffi_requests

    data, session = _tikwm_data(url, proxy)
    active_session = session
    fallback_sessions = []
    # The API and signed image CDN take different network paths. WARP can
    # reach some image hosts while a healthy TikTok xray exit reaches others.
    # A successful exit is reused for subsequent slides, so a 31-photo post
    # does not retry the full ladder 31 times.
    ladder = proxy_ladder("tiktok")
    fallbacks = list(dict.fromkeys(
        p for p in (_TIKWM_MEDIA_PROXY, *_TIKWM_CDN_XRAY_EXITS, proxy, *ladder) if p
    ))
    current_proxy = getattr(session, "proxies", {}).get("https")
    fallbacks = [p for p in fallbacks if p != current_proxy]
    fallback_index = 0
    deadline = time.monotonic() + _TIKWM_ALBUM_SECONDS
    try:
        images = data.get("images")
        if not isinstance(images, list) or not images:
            raise DownloadFailedError("Резервный сервис не вернул фото.")
        if len(images) > _TIKWM_ALBUM_MAX_ITEMS:
            raise AlbumLimitError("В посте больше 40 фото; отправка ограничена.")
        urls = [_tikwm_media_url(image) for image in images]
        sources = extract_live_photo_sources(data)
        paths: list[Path] = []
        live_photos = {}
        total_bytes = 0

        def fetch_with_routes(media_url: str, path: Path, limit: int) -> None:
            nonlocal active_session, fallback_index
            while True:
                try:
                    fetched = _stream_to_file(
                        active_session, media_url, path, limit, None, None, {}, None,
                        referer="https://www.tikwm.com/", allow_redirects=False,
                    )
                except (cffi_requests.exceptions.RequestException, StalledTransferError):
                    fetched = False
                if fetched:
                    return
                if fallback_index >= len(fallbacks) or time.monotonic() >= deadline:
                    raise DownloadFailedError(
                        "Резервный сервис не отдал медиа через доступные маршруты."
                    )
                media_proxy = fallbacks[fallback_index]
                fallback_index += 1
                active_session = cffi_requests.Session(
                    impersonate="firefox133",
                    proxies={"http": media_proxy, "https": media_proxy}, timeout=10,
                )
                fallback_sessions.append(active_session)

        for index, media_url in enumerate(urls, 1):
            if time.monotonic() >= deadline:
                raise AlbumLimitError("Превышено время загрузки фото-поста TikTok.")
            path = Path(tmpdir) / f"photo-{index}"

            image_limit = min(
                _TIKWM_PHOTO_MAX_BYTES,
                _TIKWM_ALBUM_MAX_BYTES - total_bytes + 1,
            )

            fetch_with_routes(media_url, path, image_limit)
            extension = _tikwm_photo_extension(path)
            if extension is None:
                raise DownloadFailedError("Резервный сервис отдал повреждённое фото.")
            renamed = path.with_suffix(extension)
            path.rename(renamed)
            paths.append(renamed)
            source = sources[index - 1]
            if source.motion_urls:
                motion = Path(tmpdir) / f"motion-{index}.mp4"
                try:
                    fetch_with_routes(_tikwm_media_url(source.motion_urls[0]), motion,
                                      _TIKWM_ALBUM_MAX_BYTES - total_bytes)
                except DownloadFailedError as exc:
                    raise LivePhotoError("Не удалось получить движение Live Photo.") from exc
            else:
                motion = extract_embedded_motion(
                    renamed, Path(tmpdir) / f"embedded-motion-{index}.mp4",
                )
            if motion is not None:
                live_photos[index - 1] = prepare_live_photo(renamed, motion, Path(tmpdir))
                total_bytes += motion.stat().st_size
            total_bytes += renamed.stat().st_size
            if total_bytes > _TIKWM_ALBUM_MAX_BYTES:
                raise AlbumLimitError("Фото-пост TikTok превышает лимит 80 МБ.")
        music = None
        raw_music = data.get("music")
        if isinstance(raw_music, str) and raw_music and time.monotonic() < deadline:
            try:
                music_url = _tikwm_media_url(raw_music)
                candidate = Path(tmpdir) / "music.mp3"
                if _stream_to_file(
                    active_session, music_url, candidate, _TIKWM_MUSIC_MAX_BYTES,
                    None, None, {}, None, referer="https://www.tikwm.com/",
                    allow_redirects=False,
                ):
                    with candidate.open("rb") as handle:
                        prefix = handle.read(12)
                    if prefix.startswith(b"ID3") or (
                        len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
                    ):
                        music = candidate
            except (DownloadFailedError, cffi_requests.exceptions.RequestException,
                    StalledTransferError, OSError):
                logger.info("TikTok carousel music unavailable for %s", url)
        return AlbumMedia(
            items=paths, music=music, description=data.get("title") or None,
            live_photos=live_photos,
        )
    finally:
        session.close()
        for fallback_session in fallback_sessions:
            fallback_session.close()


def _clear_directory(path: str) -> None:
    """Empty a directory without removing it."""
    for entry in Path(path).iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _album_exits(platform: str, proxy: str | None) -> list[str | None]:
    """Exits to try for a carousel, first one first.

    Only TikTok has more than one: it is the platform whose exits are refused
    or die mid-session, and the only one with a ladder of them to walk.
    """
    if platform != "tiktok":
        return [proxy]
    ladder = [exit_proxy for exit_proxy in proxy_ladder(platform) if exit_proxy != proxy]
    # Four at most: gallery-dl is a subprocess with a timeout of its own, so the
    # walk has to stay inside the patience of whoever sent the link rather than
    # exhausting every rung.
    return [proxy, *ladder][:_ALBUM_MAX_EXITS]


def is_photo_post_url(url: str) -> bool:
    """Links that are image/carousel posts and should bypass the yt-dlp video path.

    Instagram /p/ posts can mix photos and videos (yt-dlp would grab only one
    video and silently drop the rest); TikTok /photo/ and rule34.xxx posts are
    not supported by yt-dlp at all.
    """
    host = (urlparse(url).hostname or "").lower()
    path = urlparse(url).path
    if youtube_posts.is_post_url(url):
        return True
    if host.endswith("rule34.xxx"):
        return True
    if host == "pinterest.com" or host.endswith(".pinterest.com") or host == "pin.it":
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
_SPOTIFY_METADATA_DEADLINE = 24.0
_SPOTIFY_METADATA_PROXY_BASE = os.getenv(
    "SPOTIFY_METADATA_PROXY_BASE", "socks5h://xray"
)
_SPOTIFY_METADATA_PROXY_PORTS = os.getenv(
    "SPOTIFY_METADATA_PROXY_PORTS", "2077,2076,2075,2074,2073"
)
_SPOTIFY_AUDIO_PROXY = os.getenv(
    "SPOTIFY_AUDIO_PROXY", "socks5h://warp:1080"
)


def _spotify_metadata_proxies() -> list[str | None]:
    """A small fixed exit ladder; no arbitrary pool scan for one track."""
    proxies: list[str | None] = [None]
    for raw_port in _SPOTIFY_METADATA_PROXY_PORTS.split(",")[:5]:
        try:
            port = int(raw_port.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535:
            proxies.append(f"{_SPOTIFY_METADATA_PROXY_BASE}:{port}")
    return proxies


def _spotify_page_metadata(
    page_text: str, title: str | None
) -> tuple[str, str, int] | None:
    description_match = re.search(
        r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', page_text
    )
    duration_match = re.search(
        r'<meta[^>]+name="music:duration"[^>]+content="(\d+)"', page_text
    )
    page_title_match = re.search(
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', page_text
    )
    if not (description_match and duration_match and page_title_match):
        return None
    page_title = html.unescape(page_title_match.group(1)).strip()
    if title and page_title.casefold() != title.casefold():
        return None
    description_parts = html.unescape(description_match.group(1)).split(" · ")
    if not title and page_title.casefold() not in {
        part.strip().casefold() for part in description_parts[1:]
    }:
        return None
    artists = description_parts[0].strip()
    duration = int(duration_match.group(1))
    return (page_title, artists, duration) if artists and duration > 0 else None


def _spotify_stream_metadata(
    url: str, title: str | None, proxy: str | None, timeout: float, headers: dict
) -> tuple[str, str, int] | None:
    """Stop reading once the head contains the track's verified meta tags."""
    page = _curl_get(
        url, proxy=proxy, impersonate="chrome", timeout=timeout,
        headers=headers, stream=True,
    )
    try:
        page.raise_for_status()
        body = bytearray()
        for chunk in page.iter_content(chunk_size=16384):
            body.extend(chunk)
            if len(body) > 128 * 1024:
                break
            parsed = _spotify_page_metadata(body.decode("utf-8", errors="replace"), title)
            if parsed:
                return parsed
    finally:
        page.close()
    return None


def _spotify_fallback_metadata(url: str) -> tuple[str, str, int]:
    """Read public Spotify metadata without relying on spotdl's web parser."""
    deadline = time.monotonic() + _SPOTIFY_METADATA_DEADLINE
    title = None
    try:
        embed = _curl_get(
            "https://open.spotify.com/oembed", params={"url": url},
            impersonate="chrome", timeout=3,
        )
        embed.raise_for_status()
        title = str(embed.json().get("title") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - page metadata can supply title
        logger.info("Spotify oEmbed unavailable: %s", exc)
    # Spotify serves a generic desktop shell with no track metadata to Chrome.
    # In some regions even the mobile page is a generic 451 page. A few
    # dedicated xray exits can reach the public Open Graph metadata.
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    }
    for proxy in _spotify_metadata_proxies():
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        try:
            parsed = _spotify_stream_metadata(
                url, title, proxy, min(4.0, remaining), headers,
            )
        except Exception as exc:  # noqa: BLE001 - exit failures are expected
            logger.info("Spotify metadata exit failed (%s): %s", proxy, exc)
            continue
        if parsed:
            return parsed
    raise DownloadFailedError(
        "Spotify: не удалось проверить название и исполнителя трека."
    )


def _spotify_fallback_video(url: str) -> tuple[str, str]:
    title, artists, duration = _spotify_fallback_metadata(url)
    query = f"ytsearch5:{artists} {title}"
    options = _base_options(None, "https://www.youtube.com/") | {
        "skip_download": True, "retries": 0, "extractor_retries": 0,
        "socket_timeout": 12,
    }
    info = _extract_info(options, query, download=False, attempts=1) or {}
    first_artist = re.sub(r"[^\w]+", "", artists.split(",", 1)[0]).casefold()
    title_words = [
        word for word in re.findall(r"\w+", title.casefold()) if len(word) > 1
    ]
    for entry in info.get("entries") or []:
        if not entry:
            continue
        candidate_title = str(entry.get("title") or "").casefold()
        candidate_text = re.sub(
            r"[^\w]+", "",
            candidate_title + " " + str(entry.get("uploader") or "").casefold(),
        )
        candidate_words = set(re.findall(r"\w+", candidate_title))
        candidate_duration = entry.get("duration") or 0
        if (
            first_artist in candidate_text
            and all(word in candidate_words for word in title_words)
            and abs(candidate_duration - duration) <= max(6, min(15, duration * 0.05))
            and entry.get("webpage_url")
        ):
            return str(entry["webpage_url"]), title
    raise DownloadFailedError(
        "Spotify: не найдено точное совпадение трека по названию и длительности."
    )


def _download_spotify_sync(
    url: str, tmpdir: str, max_bytes: int, progress: ProgressState | None = None,
) -> tuple[Path, str]:
    if progress is not None:
        progress.phase = PHASE_RESOLVE
        progress.started_at = time.monotonic()
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
            command, capture_output=True, text=True, timeout=90, env=env, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise DownloadFailedError("Spotify: превышено время ожидания.") from exc
    files = sorted(p for p in Path(tmpdir).rglob("*.mp3") if p.is_file())
    verified_title = ""
    if not files:
        detail = (result.stderr or result.stdout or "").strip()
        logger.info(
            "spotdl produced no file (rc=%d): %s", result.returncode, detail[:500]
        )
        video_url, verified_title = _spotify_fallback_video(url)
        if progress is not None:
            progress.phase = PHASE_DOWNLOAD
            progress.started_at = time.monotonic()
        guard = {"limit": max_bytes, "tripped": None}
        options = _base_options(None, video_url) | {
            "format": _AUDIO_FORMAT,
            "outtmpl": str(Path(tmpdir) / "fallback.%(ext)s"),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "progress_hooks": [_progress_hook(progress, None, guard)],
            "proxy": _SPOTIFY_AUDIO_PROXY,
            "retries": 0,
            "fragment_retries": 0,
            "extractor_retries": 0,
            "file_access_retries": 0,
            "socket_timeout": 12,
        }
        try:
            _extract_info(options, video_url, download=True, attempts=1)
        except YtdlpDownloadError as exc:
            if guard.get("disk_low"):
                raise DownloadFailedError(
                    "Spotify: недостаточно свободного места для загрузки."
                ) from exc
            raise DownloadFailedError(
                "Spotify: не удалось скачать аудио — источник временно недоступен."
            ) from exc
        files = sorted(p for p in Path(tmpdir).rglob("*.mp3") if p.is_file())
        if not files:
            raise DownloadFailedError("Не удалось скачать трек из Spotify.")
    path = max(files, key=lambda p: p.stat().st_size)
    return path, verified_title or path.stem


@asynccontextmanager
async def _spotify_download(
    url: str, max_bytes: int, progress: ProgressState | None = None,
) -> AsyncIterator[Media]:
    tmpdir = tempfile.mkdtemp(prefix="tg-spotify-")
    try:
        path, title = await asyncio.to_thread(
            _download_spotify_sync, url, tmpdir, max_bytes, progress
        )
        size = path.stat().st_size
        if size > max_bytes:
            raise FileTooLargeError(size, max_bytes)
        yield Media(
            path=path, title=title, file_size=size,
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
    """Yield the extracted audio and remove its temporary files afterward."""
    if detect_platform(url) == "spotify":
        return _spotify_download(url, max_bytes, progress)
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
