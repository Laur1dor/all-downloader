"""Isolated yt-dlp/ffmpeg worker for bounded YouTube section downloads."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from bot.downloader import (
    DownloadCancelledError,
    DownloadFailedError,
    FileTooLargeError,
    OversizedError,
    download_video,
)


async def _run(config: dict) -> dict:
    directory = Path(config["directory"])
    async with download_video(
        config["url"],
        Path(config["cookies_file"]) if config["cookies_file"] else None,
        max_bytes=int(config["max_bytes"]),
        capped=bool(config["capped"]),
        format_override=config["format_override"],
        force_proxy=bool(config["force_proxy"]),
        ratelimit=config["ratelimit"],
        time_range=tuple(config["time_range"]),
    ) as media:
        destination = directory / f"source{media.path.suffix}"
        os.replace(media.path, destination)
        return {
            "filename": destination.name, "title": media.title,
            "duration": media.duration, "width": media.width,
            "height": media.height, "description": media.description,
        }


def main() -> int:
    config = json.load(sys.stdin)
    result_file = Path(config["directory"]) / "result.json"
    try:
        result = asyncio.run(_run(config))
        status = 0
    except (DownloadFailedError, DownloadCancelledError) as exc:
        result = {
            "error": str(exc), "error_class": type(exc).__name__,
            "retry_via_proxy": bool(getattr(exc, "retry_via_proxy", False)),
        }
        if isinstance(exc, OversizedError | FileTooLargeError):
            result["size_bytes"] = exc.size_bytes
        status = 1
    except Exception:  # noqa: BLE001 - worker reports a bounded public error
        result = {"error": "Не удалось скачать фрагмент видео."}
        status = 1
    result_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
