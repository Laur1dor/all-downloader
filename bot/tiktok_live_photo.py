"""Keep TikTok's explicitly paired photo/motion assets together for Telegram."""

from __future__ import annotations

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from bot.media_parts import MediaPreparationError, _run_ffmpeg
from bot.urlguard import ensure_public_url

MAX_LIVE_BYTES = 10_000_000
MAX_LIVE_SECONDS = 10
_XMP_SCAN_BYTES = 1024 * 1024
_MAX_EMBEDDED_BYTES = 512 * 1024 * 1024
_CAMERA_NS = "{http://ns.google.com/photos/1.0/camera/}"
_CONTAINER_NS = "{http://ns.google.com/photos/1.0/container/}"
_ITEM_NS = "{http://ns.google.com/photos/1.0/container/item/}"


def extract_embedded_motion(photo: Path, destination: Path) -> Path | None:
    """Extract only a declared Android Motion Photo tail, keeping the source.

    The official container directory places the MotionPhoto item last. Legacy
    MicroVideoOffset likewise measures from EOF. Never search arbitrary JPEG
    bytes for a video signature and mistake an unrelated payload for motion.
    """
    with photo.open("rb") as source:
        header = source.read(_XMP_SCAN_BYTES)
    packets = [match.group() for match in re.finditer(
        rb"<(?P<tag>(?:[\w-]+:)?xmpmeta)\b.*?</(?P=tag)\s*>", header, re.DOTALL,
    )]
    if not packets:
        packets = [match.group() for match in re.finditer(
            rb"<(?P<tag>(?:[\w-]+:)?RDF)\b.*?</(?P=tag)\s*>", header, re.DOTALL,
        )]
    relevant = [packet for packet in packets if b"http://ns.google.com/photos/1.0/" in packet]
    if not relevant:
        if b"http://ns.google.com/photos/1.0/camera/" in header and any(
            name in header for name in (b"MotionPhoto", b"MicroVideoOffset")
        ):
            raise LivePhotoError("Повреждены метаданные движения Live Photo.")
        return None
    if len(relevant) != 1 or b"<!DOCTYPE" in header.upper() or b"<!ENTITY" in header.upper():
        raise LivePhotoError("Неоднозначные метаданные движения Live Photo.")
    try:
        root = ET.fromstring(relevant[0])
        camera = {}
        for element in root.iter():
            values = dict(element.attrib)
            if element.tag.startswith(_CAMERA_NS):
                values[element.tag] = element.text or ""
            for key, value in values.items():
                if key.startswith(_CAMERA_NS):
                    name = key[len(_CAMERA_NS):]
                    if name in camera and camera[name] != value:
                        raise ValueError("Conflicting camera properties")
                    camera[name] = value
        modern = "MotionPhoto" in camera
        flag = camera.get("MotionPhoto") if modern else camera.get("MicroVideo")
        if flag is not None and int(flag) != 1:
            return None
        if flag is None:
            if "MicroVideoOffset" in camera:
                raise ValueError("Motion offset without declared motion flag")
            return None
        if modern:
            directories = list(root.iter(_CONTAINER_NS + "Directory"))
            if len(directories) != 1:
                raise ValueError("Missing container directory")
            items = [dict(element.attrib) for element in directories[0].iter()
                     if _ITEM_NS + "Semantic" in element.attrib]
            if not items or items[0].get(_ITEM_NS + "Semantic") != "Primary":
                raise ValueError("Missing primary photo")
            if sum(item[_ITEM_NS + "Semantic"] == "Primary" for item in items) != 1:
                raise ValueError("Multiple primary photos")
            if items[0].get(_ITEM_NS + "Mime") not in ("image/jpeg", "image/heic", "image/avif"):
                raise ValueError("Invalid primary MIME")
            motions = [item for item in items if item[_ITEM_NS + "Semantic"] == "MotionPhoto"]
            if len(motions) != 1 or motions[0] is not items[-1]:
                raise ValueError("Motion must be the final item")
            if motions[0].get(_ITEM_NS + "Mime") not in ("video/mp4", "video/quicktime"):
                raise ValueError("Invalid motion MIME")
            length = int(motions[0][_ITEM_NS + "Length"])
        else:
            length = int(camera["MicroVideoOffset"])
        size = photo.stat().st_size
        if not 16 <= length <= min(_MAX_EMBEDDED_BYTES, size - len(relevant[0])):
            raise ValueError("Invalid motion bounds")
        if size - length < header.index(relevant[0]) + len(relevant[0]):
            raise ValueError("Motion overlaps XMP metadata")
    except (ET.ParseError, ValueError, KeyError) as exc:
        raise LivePhotoError("Некорректные метаданные движения Live Photo.") from exc
    if destination.resolve() == photo.resolve():
        raise LivePhotoError("Извлечение движения не должно заменять оригинал.")
    with photo.open("rb") as source:
        source.seek(size - length)
        beginning = source.read(16)
        box_size = int.from_bytes(beginning[:4], "big")
        if beginning[4:8] != b"ftyp" or not 16 <= box_size <= length:
            raise LivePhotoError("В Live Photo отсутствует заявленный видеоконтейнер.")
        source.seek(size - length)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with destination.open("xb") as output:
                created = True
                remaining = length
                while remaining:
                    chunk = source.read(min(512 * 1024, remaining))
                    if not chunk:
                        raise LivePhotoError("Видеоконтейнер Live Photo обрезан.")
                    output.write(chunk)
                    remaining -= len(chunk)
            stream, info = _probe(destination)
            _duration(stream, info)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
    return destination


class LivePhotoError(MediaPreparationError):
    pass


@dataclass(frozen=True)
class LivePhotoSource:
    index: int
    photo_urls: tuple[str, ...]
    motion_urls: tuple[str, ...]


@dataclass(frozen=True)
class PreparedLivePhoto:
    photo: Path
    motion: Path
    original_photo: Path
    original_motion: Path
    duration: float
    width: int
    height: int


def _urls(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list):
        return tuple(dict.fromkeys(url for item in value for url in _urls(item)))
    if isinstance(value, dict):
        for key in ("urlList", "url_list", "url", "URL", "playAddr", "play_addr",
                    "play_addr_h264", "downloadAddr", "download_addr"):
            urls = _urls(value.get(key))
            if urls:
                return urls
    return ()


def _checked(urls: tuple[str, ...]) -> tuple[str, ...]:
    for url in urls:
        try:
            parsed = urlparse(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and parsed.port in (None, 80, 443))
        except ValueError:
            valid = False
        if not valid:
            raise LivePhotoError("TikTok вернул некорректную ссылку Live Photo.")
        ensure_public_url(url)
    return urls


def extract_live_photo_sources(item: dict) -> list[LivePhotoSource]:
    """Pair only motion embedded in each image's metadata, never post BGM/video.

    Web imagePost and mobile image_post_info use the same image order. Empty
    motion fields mean a static slide; an advertised but missing motion asset
    is an error rather than a successful, silently flattened Live Photo.
    """
    post = item.get("imagePost") or item.get("image_post_info") or item
    if not isinstance(post, dict):
        raise LivePhotoError("TikTok вернул некорректные данные фото-поста.")
    images = post.get("images") or []
    if not isinstance(images, list) or len(images) > 100:
        raise LivePhotoError("TikTok вернул некорректный список фото.")
    parallel_motion = post.get("live_images")
    if parallel_motion is not None and (
        not isinstance(parallel_motion, list) or len(parallel_motion) != len(images)
        or any(value is not None and (not isinstance(value, str) or not value)
               for value in parallel_motion)
    ):
        raise LivePhotoError("TikTok вернул неверное соответствие фото и движения.")
    result = []
    for index, image in enumerate(images):
        if isinstance(image, str):
            photo_urls, motion_urls = _urls(image), ()
        elif isinstance(image, dict):
            photo_urls = _urls(image.get("imageURL") or image.get("display_image")
                               or image.get("image_url") or image.get("url_list")
                               or image.get("url"))
            motion_urls = ()
            advertised = any(image.get(key) is True for key in
                             ("isLivePhoto", "is_live_photo", "livePhoto", "live_photo"))
            for key in ("videoURL", "video", "video_in_image", "livePhotoURL",
                        "live_photo_url", "motionPhotoURL", "livePhotoVideo"):
                value = image.get(key)
                if not value or value is False:
                    continue
                candidates = _urls(value)
                if not candidates:
                    raise LivePhotoError("TikTok не отдал движение одного из Live Photo.")
                if motion_urls and candidates != motion_urls:
                    raise LivePhotoError("Неоднозначное соответствие фото и движения TikTok.")
                motion_urls = candidates
            if advertised and not motion_urls:
                raise LivePhotoError("TikTok не отдал движение одного из Live Photo.")
        else:
            raise LivePhotoError("TikTok вернул некорректное фото.")
        if parallel_motion is not None and parallel_motion[index] is not None:
            paired_motion = _urls(parallel_motion[index])
            if motion_urls and paired_motion != motion_urls:
                raise LivePhotoError("Неоднозначное соответствие фото и движения TikTok.")
            motion_urls = paired_motion
        if not photo_urls:
            raise LivePhotoError("TikTok не отдал исходное фото для Live Photo.")
        result.append(LivePhotoSource(index, _checked(photo_urls), _checked(motion_urls)))
    if any(post.get(key) is True for key in ("hasLivePhoto", "has_live_photo")) and not any(
        source.motion_urls for source in result
    ):
        raise LivePhotoError("TikTok сообщил о Live Photo, но не отдал движение.")
    return result


def _probe(path: Path) -> tuple[dict, dict]:
    try:
        process = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json",
             str(path)], capture_output=True, text=True, timeout=20, check=False,
        )
        if process.returncode:
            raise ValueError(process.stderr[-200:])
        data = json.loads(process.stdout)
        stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
        if not int(stream["width"]) > 0 or not int(stream["height"]) > 0:
            raise ValueError("Invalid dimensions")
        info = dict(data.get("format") or {})
        info["audio_codecs"] = [s.get("codec_name") for s in data["streams"]
                                if s.get("codec_type") == "audio"]
        return stream, info
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration) as exc:
        raise LivePhotoError("Не удалось проверить медиа Live Photo.") from exc


def _duration(stream: dict, info: dict) -> float:
    try:
        duration = float(info.get("duration") or stream.get("duration") or 0)
    except (ValueError, TypeError) as exc:
        raise LivePhotoError("Не удалось определить длительность Live Photo.") from exc
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_LIVE_SECONDS:
        raise LivePhotoError("Движение Live Photo должно длиться не более 10 секунд.")
    return duration


def prepare_live_photo(
    photo: Path, motion: Path, output_dir: Path, cancel: Event | None = None,
) -> PreparedLivePhoto:
    """Prepare Telegram-compatible derivatives without touching source assets."""
    if photo.resolve() == motion.resolve():
        raise LivePhotoError("Фото и движение Live Photo должны быть разными файлами.")
    photo_stream, _ = _probe(photo)
    motion_stream, motion_info = _probe(motion)
    duration = _duration(motion_stream, motion_info)
    width, height = int(photo_stream["width"]), int(photo_stream["height"])
    if max(width, height) > 20 * min(width, height):
        raise LivePhotoError("Пропорции фото Live Photo не подходят для Telegram.")
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_photo = photo
    if (photo_stream.get("codec_name") not in ("mjpeg", "png")
            or width + height > 10000 or photo.stat().st_size > MAX_LIVE_BYTES):
        prepared_photo = output_dir / "live-photo.jpg"
        if prepared_photo.resolve() in (photo.resolve(), motion.resolve()):
            raise LivePhotoError("Подготовка Live Photo не должна заменять оригинал.")
        _run_ffmpeg([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2",
            "-i", str(photo), "-frames:v", "1", "-vf",
            "scale=4096:4096:force_original_aspect_ratio=decrease", "-q:v", "3",
            str(prepared_photo),
        ], 60, cancel)
    prepared_motion = output_dir / "live-motion.mp4"
    if prepared_motion.resolve() in (photo.resolve(), motion.resolve()):
        raise LivePhotoError("Подготовка Live Photo не должна заменять оригинал.")
    bitrate = min(8_000_000, int(MAX_LIVE_BYTES * 0.85 * 8 / duration))
    hdr = motion_stream.get("color_transfer") in ("smpte2084", "arib-std-b67")
    filters = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if hdr:
        filters = (
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable,zscale=t=bt709:m=bt709:r=tv,format=yuv420p," + filters
        )
    compatible = (motion_stream.get("codec_name") == "h264"
                  and motion_stream.get("pix_fmt") == "yuv420p" and not hdr
                  and motion.stat().st_size <= MAX_LIVE_BYTES
                  and all(codec == "aac" for codec in motion_info.get("audio_codecs", [])))
    codec_args = ["-c", "copy"] if compatible else [
        "-vf", filters, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
        "-c:a", "aac", "-b:a", "128k",
    ]
    _run_ffmpeg([
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2",
        "-filter_threads", "1", "-i", str(motion), "-map", "0:v:0", "-map", "0:a:0?",
        *codec_args, "-movflags", "+faststart", str(prepared_motion),
    ], 90, cancel)
    checked_motion, checked_info = _probe(prepared_motion)
    _duration(checked_motion, checked_info)
    if any(not path.is_file() or not 0 < path.stat().st_size <= MAX_LIVE_BYTES
           for path in (prepared_photo, prepared_motion)):
        raise LivePhotoError("Live Photo превышает лимит Telegram 10 МБ.")
    return PreparedLivePhoto(
        prepared_photo, prepared_motion, photo, motion, duration,
        int(checked_motion["width"]), int(checked_motion["height"]),
    )
