"""Ordered, uncropped images from public YouTube community posts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

_POST_PATH = re.compile(r"/post/(Ug[A-Za-z0-9_-]{8,126})/?$")
_COMMUNITY_PATH = re.compile(r"/(?:channel/[A-Za-z0-9_-]+|@[A-Za-z0-9_.-]+)/community/?$")
_INITIAL_DATA = re.compile(
    r'(?:\bytInitialData\s*=|window\s*\[\s*["\']ytInitialData["\']\s*\]\s*=)\s*'
)
_PAGE_LIMIT = 8 * 1024 * 1024
_IMAGE_HOSTS = {"yt3.ggpht.com", "yt3.googleusercontent.com", "i.ytimg.com"}


class YouTubePostError(ValueError):
    """The requested post cannot be downloaded as a complete photo album."""


@dataclass(frozen=True)
class YouTubePost:
    image_urls: tuple[str, ...]
    description: str | None = None


def post_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            return None
        match = _POST_PATH.fullmatch(parsed.path)
        if match:
            return match[1]
        if _COMMUNITY_PATH.fullmatch(parsed.path):
            identifiers = parse_qs(parsed.query).get("lb", [])
            if len(identifiers) == 1 and _POST_PATH.fullmatch("/post/" + identifiers[0]):
                return identifiers[0]
        return None
    except ValueError:
        return None


def canonical_post_url(url: str) -> str | None:
    identifier = post_id(url)
    return f"https://www.youtube.com/post/{identifier}" if identifier else None


def is_post_url(url: str) -> bool:
    return post_id(url) is not None


def _walk(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield item
            pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))


def _original_image(renderer: dict) -> str:
    image = renderer.get("image")
    thumbnails = image.get("thumbnails", []) if isinstance(image, dict) else []
    if not isinstance(thumbnails, list):
        thumbnails = []
    candidates = []
    for thumbnail in thumbnails:
        if not isinstance(thumbnail, dict):
            continue
        url = thumbnail.get("url")
        if not isinstance(url, str):
            continue
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https" or parsed.hostname not in _IMAGE_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443} or not parsed.path.strip("/")
            ):
                continue
        except ValueError:
            continue
        # Google image transforms include a square crop in the web carousel.
        # s0 serves the original dimensions without that crop or WebP conversion.
        if parsed.hostname in {"yt3.ggpht.com", "yt3.googleusercontent.com"}:
            url = urlunsplit((
                "https", parsed.hostname, parsed.path.split("=", 1)[0] + "=s0", "", "",
            ))
        width = thumbnail.get("width", 0)
        height = thumbnail.get("height", 0)
        area = width * height if isinstance(width, int) and isinstance(height, int) else 0
        candidates.append((area, url))
    if not candidates:
        raise YouTubePostError("Не удалось получить все фотографии поста YouTube.")
    return max(candidates, key=lambda item: item[0])[1]


def parse_post_html(html: str, identifier: str) -> YouTubePost:
    if len(html) > _PAGE_LIMIT:
        raise YouTubePostError("Ответ YouTube превышает допустимый размер.")
    decoder = json.JSONDecoder()
    for match in _INITIAL_DATA.finditer(html):
        try:
            data, _ = decoder.raw_decode(html, match.end())
        except (ValueError, RecursionError):
            continue
        for item in _walk(data):
            post = item.get("backstagePostRenderer")
            if not isinstance(post, dict) or post.get("postId") != identifier:
                continue
            attachment = post.get("backstageAttachment", {})
            if not isinstance(attachment, dict):
                raise YouTubePostError("В этом посте YouTube нет доступных фотографий.")
            multi = attachment.get("postMultiImageRenderer")
            if isinstance(multi, dict):
                images = multi.get("images")
                if not isinstance(images, list) or not images:
                    raise YouTubePostError("В посте YouTube не найдены фотографии.")
                renderers = [
                    image.get("backstageImageRenderer") if isinstance(image, dict) else None
                    for image in images
                ]
            else:
                renderers = [attachment.get("backstageImageRenderer")]
            if not renderers or any(not isinstance(image, dict) for image in renderers):
                raise YouTubePostError("В этом посте YouTube нет доступных фотографий.")
            if len(renderers) > 40:
                raise YouTubePostError("В посте YouTube слишком много фотографий.")
            urls = tuple(_original_image(image) for image in renderers)
            text = post.get("contentText", {})
            description = text.get("simpleText") if isinstance(text, dict) else None
            if isinstance(text, dict) and isinstance(text.get("runs"), list):
                description = "".join(
                    run["text"] for run in text["runs"]
                    if isinstance(run, dict) and isinstance(run.get("text"), str)
                )
            description = description.strip() or None if isinstance(description, str) else None
            return YouTubePost(urls, description)
    raise YouTubePostError(
        "Не удалось прочитать фотографии поста YouTube. Возможно, пост недоступен."
    )


def resolve_post(url: str, proxy: str | None = None) -> YouTubePost:
    canonical = canonical_post_url(url)
    if canonical is None:
        raise YouTubePostError("Некорректная ссылка на пост YouTube.")
    from curl_cffi import requests
    from curl_cffi.requests.exceptions import RequestException

    try:
        return _fetch_post(requests, canonical, proxy)
    except (RequestException, OSError) as exc:
        raise YouTubePostError("Не удалось загрузить пост YouTube. Попробуйте позже.") from exc


def _fetch_post(requests, canonical: str, proxy: str | None) -> YouTubePost:

    with requests.Session(impersonate="chrome") as session:
        response = session.get(
            canonical, proxy=proxy, timeout=30, stream=True, allow_redirects=False,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            response.raise_for_status()
            if response.status_code != 200:
                raise YouTubePostError("YouTube не предоставил содержимое поста.")
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                size += len(chunk)
                if size > _PAGE_LIMIT:
                    raise YouTubePostError("Ответ YouTube превышает допустимый размер.")
                chunks.append(chunk)
            html = b"".join(chunks).decode("utf-8", errors="replace")
            return parse_post_html(html, post_id(canonical))
        finally:
            response.close()
