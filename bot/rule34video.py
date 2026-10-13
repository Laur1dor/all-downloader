"""Compatibility with Rule34Video's download menu containing SVG icons."""

import re
from urllib.parse import parse_qs, urljoin, urlparse

from yt_dlp.extractor.rule34video import Rule34VideoIE as _Rule34VideoIE
from yt_dlp.utils import clean_html, extract_attributes, get_element_by_class, parse_filesize

from bot.urlguard import ensure_public_url


def download_formats(webpage: str, page_url: str) -> list[dict]:
    formats = []
    seen = set()
    for anchor in re.finditer(r"<a\b[^>]*>.*?</a>", webpage, re.DOTALL | re.IGNORECASE):
        element = anchor.group()
        href = extract_attributes(element).get("href", "")
        media_url = urljoin(page_url, href)
        parsed = urlparse(media_url)
        try:
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme != "https"
            or parsed.hostname != "rule34video.com"
            or parsed.username or parsed.password
            or port not in (None, 443)
            or not parsed.path.startswith("/get_file/")
            or parse_qs(parsed.query).get("download") != ["true"]
        ):
            continue
        label = clean_html(element) or ""
        match = re.search(r"\b(MP4|WEBM)\s+(\d{3,4})p", label, re.IGNORECASE)
        if not match or media_url in seen:
            continue
        ensure_public_url(media_url)
        seen.add(media_url)
        ext, height = match.group(1).lower(), int(match.group(2))
        size_label = clean_html(get_element_by_class("vp-sheet__meta", element)) or ""
        size = None
        if re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*(?:B|[KMGT]i?B)\s*", size_label, re.I):
            try:
                size = parse_filesize(size_label)
            except (ValueError, OverflowError):
                pass
        formats.append({
            "url": media_url, "ext": ext, "height": height, "quality": height,
            "format_id": f"{ext}-{height}",
            "http_headers": {"Referer": page_url},
            # The site rounds these display values, so they are estimates.
            "filesize_approx": size if size and size > 0 else None,
        })
    return formats


class Rule34VideoIE(_Rule34VideoIE):
    def _download_webpage(self, *args, **kwargs):
        webpage = super()._download_webpage(*args, **kwargs)
        self._download_menu_page = webpage
        return webpage

    def _real_extract(self, url):
        info = super()._real_extract(url)
        menu_formats = download_formats(self._download_menu_page, url)
        if menu_formats:
            info["formats"] = menu_formats
        if info.get("duration") is None:
            info["duration"] = self._search_json_ld(
                self._download_menu_page, info["id"], default={},
            ).get("duration")
        return info
