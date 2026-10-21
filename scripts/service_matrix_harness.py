"""Run one real download handler against the owner's existing bot chat.

This is an operator-only diagnostic. It creates a synthetic incoming update,
then uses the production router, database, proxy policy and local Bot API.
It never opens another chat and requires an explicit send flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.types import CallbackQuery, Chat, FSInputFile, Message, Update, User

from bot.config import load_settings
from bot.db import Database
from bot.downloader import detect_platform
from bot.handlers import create_root_router
from bot.handlers import download as download_handler
from bot.progress import CancelRegistry
from bot.proxy import configure_router
from bot.runtime import config
from bot.urlcache import UrlCache
from bot.telegram_outbound import OutgoingLimiter


def _probe_local_video(file: FSInputFile) -> dict:
    """Inspect the exact file passed to Telegram while it still exists."""
    path = Path(file.path)
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height:format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=12, check=False,
        )
        info = json.loads(result.stdout) if result.returncode == 0 else {}
        duration = float((info.get("format") or {}).get("duration") or 0)
        samples = []
        for position in (0.2, duration / 2, duration * 0.9):
            if duration and position >= duration:
                continue
            decoded = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{position:.2f}",
                    "-i", str(path), "-frames:v", "1", "-vf",
                    "scale=16:16,format=gray", "-f", "rawvideo", "pipe:1",
                ],
                capture_output=True, timeout=15, check=False,
            )
            if decoded.returncode == 0 and decoded.stdout:
                pixels = decoded.stdout
                samples.append({
                    "at_s": round(position, 2),
                    "mean_luma": round(sum(pixels) / len(pixels), 1),
                    "luma_range": max(pixels) - min(pixels),
                })
        return {
            "bytes": path.stat().st_size,
            "duration_s": round(duration, 2),
            "streams": [
                {key: stream.get(key) for key in (
                    "codec_type", "codec_name", "width", "height",
                ) if key in stream}
                for stream in info.get("streams") or []
            ],
            "decoded_samples": samples,
        }
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:160]}


class _Trace(BaseRequestMiddleware):
    def __init__(self, started: float, platform: str) -> None:
        self.started = started
        self.platform = platform
        self.events: list[dict] = []
        self.file_callback_data: str | None = None

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        event = {"at_s": round(time.monotonic() - self.started, 2), "api": name}
        if name in {"sendMessage", "editMessageText"}:
            event["text"] = str(getattr(method, "text", ""))[:350]
        if name == "sendMediaGroup":
            event["count"] = len(method.media)
        keyboard = getattr(method, "reply_markup", None)
        if getattr(keyboard, "inline_keyboard", None):
            event["buttons"] = [
                button.text for row in keyboard.inline_keyboard for button in row
            ]
            for row in keyboard.inline_keyboard:
                for button in row:
                    if (button.callback_data or "").startswith("ttfile:"):
                        self.file_callback_data = button.callback_data
        if name in {"sendVideo", "sendDocument"}:
            upload = getattr(method, "video" if name == "sendVideo" else "document", None)
            if isinstance(upload, FSInputFile):
                event["upload_bytes"] = Path(upload.path).stat().st_size
                event["upload_inode"] = Path(upload.path).stat().st_ino
        if name == "sendDocument":
            event["disable_content_type_detection"] = bool(
                getattr(method, "disable_content_type_detection", False)
            )
        if name == "sendVideo" and self.platform == "tiktok":
            video = getattr(method, "video", None)
            if isinstance(video, FSInputFile):
                event["source_probe"] = await asyncio.to_thread(
                    _probe_local_video, video,
                )
        began = time.monotonic()
        if name in {"sendVideo", "sendDocument", "sendAudio", "sendMediaGroup", "sendPhoto", "sendLivePhoto"}:
            print(json.dumps({"stage": "media_request", "api": name,
                              "at_s": event["at_s"], "bytes": event.get("upload_bytes")}),
                  flush=True)
        try:
            result = await make_request(bot, method)
        except Exception as exc:
            event["error"] = type(exc).__name__
            raise
        else:
            messages = result if isinstance(result, list) else [result]
            event["delivered"] = [
                "document" if getattr(item, "document", None) else
                "video" if getattr(item, "video", None) else
                "photo" if getattr(item, "photo", None) else
                "audio" if getattr(item, "audio", None) else
                "message" if getattr(item, "message_id", None) else "other"
                for item in messages
            ]
            return result
        finally:
            event["duration_s"] = round(time.monotonic() - began, 2)
            self.events.append(event)


async def _run(
    url: str, concurrent_url: str | None = None,
    second_delay: float = 15, max_source_mb: int | None = None,
    deadline_seconds: float = 300, force_download: bool = False,
    rule34video_main_primary: bool = False,
    quality_height: int | None = None,
    tiktok_file_callback: bool = False,
    tiktok_file_only: bool = False,
) -> dict:
    settings = load_settings()
    platform = detect_platform(url.split()[0])
    if platform == "other":
        raise ValueError("Only explicitly supported services are accepted")
    if (tiktok_file_callback or tiktok_file_only) and platform != "tiktok":
        raise ValueError("TikTok document callback requires a TikTok URL")
    if tiktok_file_only and not tiktok_file_callback:
        raise ValueError("--tiktok-file-only requires --tiktok-file-callback")
    if not settings.api_base_url:
        raise RuntimeError("The local Bot API is required for this diagnostic")

    started = time.monotonic()
    db = Database(settings.database_dsn)
    await db.connect(attempts=1)
    await config.load(db)
    proxy_router = configure_router(
        settings.proxy_url,
        os.getenv("GOIDA_PROXY_URL") or None,
        os.getenv("BYPASS_PROXY_URL") or None,
    )
    await proxy_router.start()
    if rule34video_main_primary:
        if platform != "rule34video":
            raise ValueError("--rule34video-main-primary requires a Rule34Video URL")
        main_proxy = proxy_router._pool_url("main")
        if not main_proxy or not proxy_router._pool_ok.get("main"):
            raise RuntimeError("The main proxy is not healthy")
        proxy_router._routing.setdefault("services", {})["rule34video"] = main_proxy
    session = AiohttpSession(
        timeout=90, api=TelegramAPIServer.from_base(settings.api_base_url),
    )
    session.middleware(OutgoingLimiter())
    trace = _Trace(started, platform)
    session.middleware(trace)
    bot = Bot(
        token=settings.bot_token, session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        before = await db.pool.fetchval(
            "SELECT coalesce(max(id), 0) FROM conversions WHERE telegram_id=$1",
            settings.admin_id,
        )
        cancel_registry = CancelRegistry()
        url_cache = UrlCache()
        dispatcher = Dispatcher(
            db=db, settings=settings, url_cache=url_cache,
            cancel_registry=cancel_registry,
        )
        dispatcher.include_router(create_root_router(settings.admin_id))
        async def feed(text: str, number: int) -> str | None:
            message = Message(
                message_id=number, date=datetime.now(timezone.utc), text=text,
                chat=Chat(id=settings.admin_id, type=ChatType.PRIVATE),
                from_user=User(id=settings.admin_id, is_bot=False, first_name="Owner"),
            )
            update = Update(update_id=int(time.time()) + number, message=message)
            try:
                await dispatcher.feed_update(bot, update)
            except Exception as exc:  # noqa: BLE001 - report handler failure
                return f"{type(exc).__name__}: {exc}"[:250]
            return None

        async def delayed_feed() -> str | None:
            await asyncio.sleep(second_delay)
            assert concurrent_url is not None
            return await feed(concurrent_url, 2)

        async def feed_file_callback() -> str | None:
            if not tiktok_file_only and trace.file_callback_data is None:
                return "TikTok file button was not present on delivered media"
            callback_data = (
                f"ttfile:{url_cache.store(url)}"
                if tiktok_file_only else trace.file_callback_data
            )
            if callback_data is None:
                return "TikTok callback URL token was not retained"
            callback = CallbackQuery(
                id="matrix-diagnostic", chat_instance="owner-only",
                from_user=User(id=settings.admin_id, is_bot=False, first_name="Owner"),
                message=Message(
                    message_id=3, date=datetime.now(timezone.utc),
                    chat=Chat(id=settings.admin_id, type=ChatType.PRIVATE),
                ),
                data=callback_data,
            )

            async def acknowledge(_callback, *args, **kwargs) -> bool:
                return True

            try:
                with patch.object(CallbackQuery, "answer", acknowledge):
                    await dispatcher.feed_update(
                        bot, Update(update_id=int(time.time()) + 4,
                                    callback_query=callback),
                    )
            except Exception as exc:  # noqa: BLE001 - report callback failure
                return f"{type(exc).__name__}: {exc}"[:250]
            return None

        async def select_quality_height() -> None:
            # Select through the handler's real quality menu, leaving its
            # estimated-size and disk-limit checks in place.
            while True:
                for event, state in tuple(download_handler._pending_choices.values()):
                    if "height" in state and not event.is_set():
                        state["height"] = quality_height
                        event.set()
                        return
                await asyncio.sleep(0.1)

        original_limit = download_handler._download_limit
        limit_context = (
            patch.object(
                download_handler, "_download_limit",
                side_effect=lambda settings, user_id: min(
                    original_limit(settings, user_id), max_source_mb * 1024**2
                ),
            ) if max_source_mb is not None else nullcontext()
        )
        cache_context = (
            patch.object(download_handler, "_send_cached_video", return_value=False)
            if force_download else nullcontext()
        )
        with limit_context, cache_context:
            transient_before = set(Path(tempfile.gettempdir()).glob("tg-tiktok-document-*"))
            album_before = set(Path(tempfile.gettempdir()).glob("tg-album-*"))
            selector = (
                asyncio.create_task(select_quality_height())
                if quality_height is not None else None
            )
            tasks = [] if tiktok_file_only else [feed(url, 1)]
            if concurrent_url is not None:
                tasks.append(delayed_feed())
            running = asyncio.gather(*tasks)
            try:
                errors = await asyncio.wait_for(
                    asyncio.shield(running), timeout=deadline_seconds,
                )
                if not errors:
                    errors = [None]
            except TimeoutError:
                # wait_for(to_thread(...)) alone only cancels the awaiter;
                # signal the download and ffmpeg watchdogs before leaving.
                for token in tuple(cancel_registry._events):
                    cancel_registry.cancel(token)
                try:
                    await asyncio.wait_for(asyncio.shield(running), timeout=60)
                except TimeoutError:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                errors = [f"TimeoutError: exceeded {deadline_seconds:g}s"]
            finally:
                if selector is not None:
                    selector.cancel()
                    await asyncio.gather(selector, return_exceptions=True)
            callback_error = None
            cache_before = None
            callback_url = url
            if tiktok_file_callback and (tiktok_file_only or errors[0] is None):
                callback_token = (trace.file_callback_data or "").removeprefix("ttfile:")
                callback_url = url_cache.get(callback_token) or url
                source = download_handler._tiktok_original_path(callback_url)
                cache_before = {
                    "exists": source is not None,
                    "inode": source.stat().st_ino if source is not None else None,
                }
                callback_error = await asyncio.wait_for(
                    feed_file_callback(), timeout=deadline_seconds,
                )
            transient_after = set(Path(tempfile.gettempdir()).glob("tg-tiktok-document-*"))
            album_after = set(Path(tempfile.gettempdir()).glob("tg-album-*"))
            cache_after = (
                download_handler._tiktok_original_path(callback_url)
                if tiktok_file_callback else None
            )
        rows = await db.pool.fetch(
            "SELECT status, source_platform, media_type, file_size, processing_time "
            "FROM conversions WHERE telegram_id=$1 AND id>$2 "
            "AND source_platform=$3 ORDER BY id",
            settings.admin_id, before, platform,
        )
        return {
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "url": url, "platform": platform,
            "quality_height": quality_height,
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": errors[0], "concurrent_url": concurrent_url,
            "concurrent_error": errors[1] if len(errors) > 1 else None,
            "conversions": [dict(row) for row in rows],
            "events": trace.events,
            "callback_error": callback_error,
            "original_cache_before_callback": cache_before,
            "original_cache_after_callback": cache_after is not None,
            "document_temp_dirs_left": len(transient_after - transient_before),
            "album_temp_dirs_left": len(album_after - album_before),
        }
    finally:
        await bot.session.close()
        await proxy_router.stop()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--send-to-owner", action="store_true", required=True)
    parser.add_argument("--concurrent-url")
    parser.add_argument("--second-delay", type=float, default=15)
    parser.add_argument("--max-source-mb", type=int)
    parser.add_argument("--deadline-seconds", type=float, default=300)
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--rule34video-main-primary", action="store_true")
    parser.add_argument("--quality-height", type=int)
    parser.add_argument("--tiktok-file-callback", action="store_true")
    parser.add_argument("--tiktok-file-only", action="store_true")
    args = parser.parse_args()
    if not args.send_to_owner:
        raise SystemExit("Explicit --send-to-owner flag is required")
    if args.max_source_mb is not None and args.max_source_mb < 10:
        raise SystemExit("--max-source-mb must be at least 10")
    if args.quality_height is not None and args.quality_height <= 0:
        raise SystemExit("--quality-height must be positive")
    # Fail before dispatching a real bot message if a restored diagnostic file
    # is owned by root or its output directory is unavailable.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8"):
        pass
    result = asyncio.run(_run(
        args.url, args.concurrent_url, args.second_delay,
        args.max_source_mb, args.deadline_seconds, args.force_download,
        args.rule34video_main_primary, args.quality_height,
        args.tiktok_file_callback, args.tiktok_file_only,
    ))
    with args.output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    print(json.dumps({
        "url": result["url"], "platform": result["platform"],
        "elapsed_s": result["elapsed_s"], "error": result["error"],
        "concurrent_error": result["concurrent_error"],
        "conversions": result["conversions"],
        "api_calls": len(result["events"]),
    }, ensure_ascii=True, default=str))


if __name__ == "__main__":
    main()
