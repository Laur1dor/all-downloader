"""Bounded preparation of independently playable Telegram MP4 fragments."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event

MAX_SOURCE_BYTES = 6 * 1024**3
MAX_PARTS = 8
MAX_DURATION = 4 * 3600
_RESERVE_BYTES = 512 * 1024**2
_WORK_SECONDS = 12 * 60
_MIN_VIDEO_BITRATE = 300_000
_MAX_VIDEO_BITRATE = 5_000_000
_AUDIO_BITRATE = 128_000
_RANGE = re.compile(r"^(\d{1,2}(?::\d{2}){1,2})\s*[-–—]\s*(\d{1,2}(?::\d{2}){1,2})$")


class MediaPreparationError(Exception):
    pass


def parse_time_range(value: str) -> tuple[int, int] | None:
    """Accept HH:MM:SS-MM:SS after a URL; reject ambiguous free text."""
    match = _RANGE.fullmatch(value.strip())
    if not match:
        return None

    def seconds(raw: str) -> int:
        fields = [int(part) for part in raw.split(":")]
        if any(part >= 60 for part in fields[1:]):
            raise ValueError("Минуты и секунды должны быть меньше 60.")
        result = 0
        for part in fields:
            result = result * 60 + part
        return result

    start, end = seconds(match[1]), seconds(match[2])
    if end <= start or end - start > MAX_DURATION:
        raise ValueError("Укажите интервал до 4 часов, где конец позже начала.")
    return start, end


def _duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr)
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise MediaPreparationError("Не удалось определить длительность видео.") from exc
    if not 0 < duration <= MAX_DURATION:
        raise MediaPreparationError("Длительность видео превышает лимит 4 часа.")
    return duration


def _run_ffmpeg(command: list[str], timeout: float, cancel: Event | None) -> None:
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise MediaPreparationError("Не удалось запустить ffmpeg.") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise MediaPreparationError("Подготовка отменена.")
            if time.monotonic() >= deadline:
                raise MediaPreparationError("Превышено время подготовки видео.")
            try:
                _, stderr = process.communicate(
                    timeout=max(0.1, min(1, deadline - time.monotonic()))
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.communicate()
        raise
    if process.returncode != 0:
        raise MediaPreparationError(
            f"Не удалось перекодировать видео: {stderr[-300:]}"
        )


def _cut(path: Path, output: Path, start: float, length: float,
         timeout: float, bitrate: int, cancel: Event | None) -> None:
    # Stream copy can begin on a non-keyframe (black screen in Telegram Web).
    # Encoding gives each part a fresh IDR frame, monotonic timestamps and a
    # codec/pixel format accepted by Telegram's web and mobile players.
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-threads", "2", "-filter_threads", "1", "-ss", str(start),
        "-i", str(path), "-t", str(length), "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", (
            "scale=w='min(iw,1920)':h='min(ih,1080)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        ),
        "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-pix_fmt", "yuv420p", "-profile:v", "main",
        "-level:v", "4.0", "-b:v", str(bitrate),
        "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
        "-g", "60", "-keyint_min", "30", "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", str(_AUDIO_BITRATE), "-ac", "2",
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, timeout, cancel)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaPreparationError("ffmpeg не создал фрагмент видео.")
    _validate(output)


def _validate(path: Path) -> None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,codec_type,pix_fmt,width,height:format=duration,format_name",
             "-of", "json", str(path)], capture_output=True, text=True,
            timeout=30, check=False,
        )
        data = json.loads(result.stdout)
        streams = data["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        valid = (
            result.returncode == 0
            and "mp4" in data["format"]["format_name"]
            and float(data["format"]["duration"]) > 0
            and video["codec_name"] == "h264"
            and video["pix_fmt"] == "yuv420p"
            and all(stream["codec_name"] == "aac" for stream in audio)
        )
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        raise MediaPreparationError("Не удалось проверить готовый MP4.") from exc
    if not valid:
        raise MediaPreparationError("Готовый MP4 не совместим с Telegram.")
    try:
        first_packet = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-read_intervals", "0%+#1", "-show_entries", "packet=flags",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaPreparationError("Не удалось проверить первый кадр MP4.") from exc
    if first_packet.returncode != 0 or not first_packet.stdout.strip().startswith("K"):
        raise MediaPreparationError("Фрагмент начинается без ключевого кадра.")


def _source_compatible(path: Path) -> bool:
    """Allow cheap keyframe-aligned copies only for already compatible media."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,codec_type,pix_fmt,width,height", "-of", "json",
             str(path)], capture_output=True, text=True, timeout=30,
            check=False,
        )
        streams = json.loads(result.stdout)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        return bool(
            result.returncode == 0
            and video["codec_name"] == "h264"
            and video["pix_fmt"] == "yuv420p"
            and int(video["width"]) <= 1920
            and int(video["height"]) <= 1080
            and all(stream["codec_name"] == "aac" for stream in audio)
        )
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError):
        return False


def _segment_copy(path: Path, output_dir: Path, segment_time: float,
                  timeout: float, cancel: Event | None) -> list[Path]:
    command = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
        "-segment_time", str(segment_time), "-reset_timestamps", "1",
        "-segment_start_number", "1", "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        str(output_dir / "part-%02d.mp4"),
    ]
    _run_ffmpeg(command, timeout, cancel)
    paths = sorted(output_dir.glob("part-*.mp4"))
    if not paths or len(paths) > MAX_PARTS:
        raise MediaPreparationError("Не удалось ограничить число частей видео.")
    for part in paths:
        _validate(part)
    return paths


def _trim_audio(path: Path, time_range: tuple[int, int],
                cancel: Event | None) -> Path:
    if cancel is not None and cancel.is_set():
        raise MediaPreparationError("Подготовка отменена.")
    duration = _duration(path)
    start, end = time_range
    if start >= duration:
        raise MediaPreparationError("Начало интервала находится за концом аудио.")
    end = min(end, duration)
    output = path.with_name("trimmed.m4a")
    _run_ffmpeg([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-threads", "2",
        "-ss", str(start), "-i", str(path), "-t", str(end - start),
        "-vn", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(output),
    ], _WORK_SECONDS, cancel)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaPreparationError("Не удалось подготовить аудиофрагмент.")
    actual = _duration(output)
    if abs(actual - (end - start)) > 1:
        raise MediaPreparationError("Аудиофрагмент имеет неверную длительность.")
    return output


async def prepare_audio(path: Path, time_range: tuple[int, int],
                        cancel: Event | None = None) -> Path:
    return await asyncio.to_thread(_trim_audio, path, time_range, cancel)


def _prepare(path: Path, limit: int, time_range: tuple[int, int] | None,
             cancel: Event | None, force_compatible: bool = False) -> list[Path]:
    if cancel is not None and cancel.is_set():
        raise MediaPreparationError("Подготовка отменена.")
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise MediaPreparationError("Исходник превышает общий лимит 6 ГБ.")
    if not time_range and size <= limit and not force_compatible:
        return [path]
    duration = _duration(path)
    start, end = time_range or (0, duration)
    if start >= duration:
        raise MediaPreparationError("Начало интервала находится за концом видео.")
    end = min(end, duration)
    # Splitting needs a second copy of the source on disk until upload finishes.
    if shutil.disk_usage(path.parent).free < size + _RESERVE_BYTES:
        raise MediaPreparationError("Недостаточно свободного места для нарезки видео.")
    target = int(limit * 0.75)  # ABR variance and MP4 overhead need headroom.
    approx_bytes = size * (end - start) / duration
    # A large source can be encoded smaller.  Do not reject it just because
    # source bitrate would need more than eight *copy* segments.
    count = min(MAX_PARTS, max(1, int(approx_bytes / target) + 1))
    deadline = time.monotonic() + _WORK_SECONDS
    output_dir = path.parent / "parts"
    output_dir.mkdir(exist_ok=True)
    fast_copy = time_range is None and not force_compatible and _source_compatible(path)
    while count <= MAX_PARTS:
        for old in output_dir.glob("part-*.mp4"):
            old.unlink()
        paths: list[Path] = []
        too_large = False
        segment_length = (end - start) / count
        if fast_copy:
            try:
                copied = _segment_copy(
                    path, output_dir, segment_length,
                    deadline - time.monotonic(), cancel,
                )
            except MediaPreparationError:
                fast_copy = False
                continue
            if all(part.stat().st_size <= limit for part in copied):
                return copied
            if count == MAX_PARTS:
                fast_copy = False
            else:
                count += 1
            continue
        bitrate = int(target * 8 / segment_length - _AUDIO_BITRATE)
        if bitrate < _MIN_VIDEO_BITRATE:
            count += 1
            continue
        bitrate = min(bitrate, _MAX_VIDEO_BITRATE)
        expected_bytes = int((bitrate + _AUDIO_BITRATE) * (end - start) / 8)
        if shutil.disk_usage(output_dir).free < expected_bytes + _RESERVE_BYTES:
            raise MediaPreparationError(
                "Недостаточно места для совместимой MP4-версии видео."
            )
        for index in range(count):
            if cancel is not None and cancel.is_set():
                raise MediaPreparationError("Подготовка отменена.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaPreparationError("Превышено время подготовки видео.")
            segment_start = start + (end - start) * index / count
            segment_end = start + (end - start) * (index + 1) / count
            output = output_dir / f"part-{index + 1:02d}.mp4"
            _cut(path, output, segment_start, segment_end - segment_start,
                 remaining, bitrate, cancel)
            if output.stat().st_size > limit:
                too_large = True
                break
            paths.append(output)
        if not too_large:
            return paths
        count += 1
    raise MediaPreparationError("Не удалось уложить фрагменты в лимит Telegram.")


async def prepare_video(path: Path, limit: int,
                        time_range: tuple[int, int] | None = None,
                        cancel: Event | None = None,
                        force_compatible: bool = False) -> list[Path]:
    if limit <= 0:
        raise MediaPreparationError("Некорректный лимит файла.")
    return await asyncio.to_thread(
        _prepare, path, limit, time_range, cancel, force_compatible,
    )


def _streaming_mp4(path: Path, cancel: Event | None) -> Path:
    """Put downloaded streams in a seekable MP4 without changing their quality."""
    if cancel is not None and cancel.is_set():
        raise MediaPreparationError("Подготовка отменена.")
    if shutil.disk_usage(path.parent).free < path.stat().st_size + _RESERVE_BYTES:
        raise MediaPreparationError("Недостаточно места для подготовки MP4.")
    output = path.with_name(f"telegram-{path.stem}.mp4")
    _run_ffmpeg([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(output),
    ], _WORK_SECONDS, cancel)
    _validate(output)
    return output


async def prepare_streaming_mp4(path: Path, cancel: Event | None = None) -> Path:
    return await asyncio.to_thread(_streaming_mp4, path, cancel)
