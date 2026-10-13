"""Bounded, tri-state inspection of TikTok's original video color transfer."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bot.urlguard import ensure_public_url

_FIRST_BYTES = 256 * 1024
_MAX_BYTES = 1024 * 1024
_MAX_SOURCE_BYTES = 2 * 1024**3
_SDR_TRANSFERS = {'bt709', 'gamma22', 'gamma28', 'smpte170m', 'smpte240m',
                  'iec61966-2-1', 'bt2020-10', 'bt2020-12'}
_MEDIA_SUFFIXES = ('tiktok.com', 'tiktokcdn.com', 'tiktokcdn-us.com', 'tiktokcdn-eu.com')


def _validate_url(url: str, *, media: bool = False) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    allowed = (any(host == s or host.endswith('.' + s) for s in _MEDIA_SUFFIXES)
               if media else host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com',
                                      'vt.tiktok.com', 'vm.tiktok.com'))
    if (parsed.scheme != 'https' or not allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError('Unexpected TikTok HDR probe host')
    ensure_public_url(url)


def _read_range(session, url: str, start: int, end: int, deadline: float) -> tuple[bytes, int]:
    _validate_url(url, media=True)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    response = session.get(url, headers={'Range': f'bytes={start}-{end}',
                                        'Referer': 'https://www.tiktok.com/'},
                           stream=True, allow_redirects=False, timeout=remaining)
    try:
        # A server ignoring Range must never trigger a complete source download.
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                             response.headers.get('Content-Range', ''))
        if response.status_code != 206 or match is None:
            raise ValueError('Source did not honor the requested byte range')
        first, last, total = map(int, match.groups())
        if (first != start or last != min(end, total - 1) or total <= last
                or total > _MAX_SOURCE_BYTES or last < first):
            raise ValueError('Invalid source byte range')
        expected = last - first + 1
        chunks = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if time.monotonic() >= deadline or len(chunk) > expected - len(chunks):
                raise ValueError('Source exceeded the range probe budget')
            chunks.extend(chunk)
            if len(chunks) == expected:
                break
        if len(chunks) != expected:
            raise ValueError('Truncated source byte range')
        return bytes(chunks), total
    finally:
        response.close()


def _transfer(path: Path, deadline: float) -> bool | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-probesize', str(_FIRST_BYTES), '-analyzeduration', '0',
         '-select_streams', 'v:0', '-show_entries', 'stream=color_transfer',
         '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=remaining, check=False,
    )
    if result.returncode:
        return None
    streams = json.loads(result.stdout).get('streams') or []
    transfer = streams[0].get('color_transfer') if streams else None
    if transfer in ('arib-std-b67', 'smpte2084'):
        return True
    if transfer in _SDR_TRANSFERS:
        return False
    return None


def probe_tiktok_item_hdr(item: dict, session, *, timeout_seconds: float = 4.0) -> bool | None:
    """Reuse native metadata/session; inspect at most 1 MiB of the best original.

    A failed probe is unknown, never proof of SDR. Codec and bit depth alone
    cannot establish HDR. A bounded tail read supports files with moov at EOF.
    """
    deadline = time.monotonic() + timeout_seconds
    try:
        from bot.downloader import _tiktok_variants

        variants = _tiktok_variants(item, original_only=True)
        if not variants:
            return None
        url = variants[0]['urls'][0]
        header, total = _read_range(session, url, 0, _FIRST_BYTES - 1, deadline)
        with tempfile.TemporaryDirectory(prefix='tg-tiktok-hdr-probe-') as directory:
            path = Path(directory) / 'source.mp4'
            path.write_bytes(header)
            hdr = _transfer(path, deadline)
            if hdr is not None or total <= len(header):
                return hdr
            # Keep offsets intact in a sparse file; only fetched bytes consume
            # disk blocks. ffprobe needs moov/hvcC, not the entire mdat.
            tail_size = min(_MAX_BYTES - len(header), total - len(header))
            tail_start = total - tail_size
            tail, checked_total = _read_range(session, url, tail_start, total - 1, deadline)
            if checked_total != total:
                return None
            with path.open('r+b') as target:
                target.truncate(total)
                target.seek(tail_start)
                target.write(tail)
            return _transfer(path, deadline)
    except Exception:
        # Signed media addresses, cookies, and transport errors stay private.
        return None


def _probe_url(url: str, cookies_file: Path | None, proxy: str | None) -> bool | None:
    deadline = time.monotonic() + 10
    session = None
    try:
        from curl_cffi import requests

        from bot.downloader import _TIKTOK_DATA_RE, _tiktok_cookies
        from bot.proxy import proxy_for

        _validate_url(url)
        session = requests.Session(impersonate='firefox133', proxy=proxy or proxy_for('tiktok'))
        session.cookies.update(_tiktok_cookies(cookies_file))
        for _ in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            response = session.get(url, timeout=min(4, remaining), allow_redirects=False,
                                   stream=True, headers={'Referer': 'https://www.tiktok.com/'})
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    _validate_url(url)
                    continue
                if response.status_code != 200:
                    return None
                body = bytearray()
                for chunk in response.iter_content(chunk_size=32768):
                    if len(body) + len(chunk) > 2 * _MAX_BYTES or time.monotonic() >= deadline:
                        return None
                    body.extend(chunk)
                match = _TIKTOK_DATA_RE.search(body.decode('utf-8', errors='replace'))
                if not match:
                    return None
                scope = json.loads(match.group(1)).get('__DEFAULT_SCOPE__', {})
                item = scope.get('webapp.video-detail', {}).get('itemInfo', {}).get('itemStruct')
                if not isinstance(item, dict):
                    return None
            finally:
                response.close()
            return probe_tiktok_item_hdr(item, session,
                                         timeout_seconds=min(4, deadline - time.monotonic()))
        return None
    except Exception:
        return None
    finally:
        if session is not None:
            session.close()


async def probe_tiktok_source_hdr(url: str, cookies_file: Path | None = None,
                                  proxy: str | None = None) -> bool | None:
    """Optional lookup for a cached SDR preview whose source metadata is unknown."""
    return await asyncio.to_thread(_probe_url, url, cookies_file, proxy)
