"""Resolve the official anonymous Yandex Music 30-second preview."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_TRACK_PATH = re.compile(r"/album/(\d+)/track/(\d+)/?")
_Y_KEY = "XGRlBW9FXlekgbPrRHuSiA"
_MAX_METADATA = 64 * 1024


class PreviewUnavailableError(ValueError):
    """The public preview could not be verified or resolved."""


@dataclass(frozen=True)
class YandexPreview:
    url: str
    title: str
    duration: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _read_metadata(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=12) as response:
        if response.status != 200:
            raise PreviewUnavailableError("Yandex Music did not return preview metadata")
        data = response.read(_MAX_METADATA + 1)
    if len(data) > _MAX_METADATA:
        raise PreviewUnavailableError("Yandex Music preview metadata is too large")
    return data


def _yandex_url(raw: str, *, storage: bool = False) -> str:
    parsed = urllib.parse.urlsplit(raw)
    host = (parsed.hostname or "").lower()
    allowed = host == "storage.mds.yandex.net" if storage else (
        host.endswith((".yandex.net", ".yandex.ru"))
    )
    if (
        parsed.scheme != "https" or not allowed or parsed.port not in (None, 443)
        or parsed.username or parsed.password
    ):
        raise PreviewUnavailableError("Yandex Music returned an invalid media address")
    return raw


def resolve_yandex_preview(url: str) -> YandexPreview:
    """Return an expiring MP3 URL; callers must label it as a 30-second preview.

    This uses only the service's anonymous API, never cookies or account tokens.
    Resolve immediately before downloading because the signed URL expires.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "music.yandex.ru", "music.yandex.kz", "music.yandex.by", "music.yandex.com",
    }:
        raise PreviewUnavailableError("Not a Yandex Music track URL")
    match = _TRACK_PATH.fullmatch(parsed.path)
    if match is None:
        raise PreviewUnavailableError("Not a single Yandex Music track")
    album_id, track_id = map(int, match.groups())

    try:
        payload = json.loads(_read_metadata(
            f"https://api.music.yandex.net/tracks/{track_id}"
        ))
        tracks = payload["result"]
        track = next(item for item in tracks if int(item["id"]) == track_id)
        albums = track.get("albums") or []
        if albums and album_id not in {int(item["id"]) for item in albums}:
            raise PreviewUnavailableError("The track does not belong to this album")
        title = str(track["title"]).strip()
        if not title:
            raise PreviewUnavailableError("Yandex Music returned an empty track title")
        artists = ", ".join(
            str(artist["name"]).strip() for artist in track.get("artists") or []
            if artist.get("name")
        )
        display_title = f"{artists} — {title}" if artists else title

        payload = json.loads(_read_metadata(
            f"https://api.music.yandex.net/tracks/{track_id}/download-info"
        ))
        variants = payload["result"]
        preview = next(
            item for item in variants
            if item.get("preview") is True and item.get("codec") == "mp3"
        )
        info_url = _yandex_url(preview["downloadInfoUrl"], storage=True)
        root = ET.fromstring(_read_metadata(info_url))
        if root.tag != "download-info":
            raise PreviewUnavailableError("Invalid Yandex Music preview metadata")
        fields = {child.tag: child.text or "" for child in root}
        host = fields["host"].strip().lower()
        path = fields["path"]
        ts = fields["ts"]
        salt = fields["s"]
        if (
            not path.startswith("/") or ".." in path
            or not re.fullmatch(r"[0-9a-f]{1,32}", ts)
            or not salt or not re.fullmatch(r"[a-z0-9.-]+", host)
        ):
            raise PreviewUnavailableError("Invalid Yandex Music preview location")
        digest = hashlib.md5((_Y_KEY + path[1:] + salt).encode()).hexdigest()
        media_url = _yandex_url(
            f"https://{host}/get-mp3/{digest}/{ts}{path}?track-id={track_id}"
        )
        return YandexPreview(media_url, display_title, 30)
    except (KeyError, TypeError, ValueError, StopIteration, ET.ParseError,
            urllib.error.URLError, TimeoutError) as exc:
        if isinstance(exc, PreviewUnavailableError):
            raise
        raise PreviewUnavailableError("Yandex Music preview is unavailable") from exc
