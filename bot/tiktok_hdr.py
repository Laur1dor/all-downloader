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
_RESOLVER_MEDIA_HOSTS = ('tikwm.com', 'www.tikwm.com')


def _validate_url(url: str, *, media: bool = False, anonymous_resolver: bool = False) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    allowed = (any(host == s or host.endswith('.' + s) for s in _MEDIA_SUFFIXES)
               if media else host in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com',
                                      'vt.tiktok.com', 'vm.tiktok.com'))
    if media and anonymous_resolver and host in _RESOLVER_MEDIA_HOSTS:
        allowed = True
    if (parsed.scheme != 'https' or not allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError('Unexpected TikTok HDR probe host')
    ensure_public_url(url)


def _read_range(session, url: str, start: int, end: int, deadline: float) -> tuple[bytes, int]:
    anonymous = getattr(session, '_tiktok_hdr_anonymous_resolver', False) is True
    if ((urlsplit(url).hostname or '').lower() in _RESOLVER_MEDIA_HOSTS
            and anonymous and bool(session.cookies)):
        raise ValueError('Resolver media requires a cookie-free session')
    _validate_url(url, media=True, anonymous_resolver=anonymous)
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


def hdr_state_from_stream(stream: dict) -> bool | None:
    """Classify actual stream metadata, independent of source/preview selection."""
    transfer = stream.get('color_transfer')
    for side_data in stream.get('side_data_list') or []:
        if (side_data.get('side_data_type') == 'DOVI configuration record'
                and side_data.get('rpu_present_flag') == 1
                and isinstance(side_data.get('dv_profile'), int)
                and side_data['dv_profile'] > 0):
            return True
    if transfer in ('arib-std-b67', 'smpte2084'):
        return True
    if transfer in _SDR_TRANSFERS:
        return False
    return None


def _transfer(path: Path, deadline: float) -> bool | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-probesize', str(_FIRST_BYTES), '-analyzeduration', '0',
         '-select_streams', 'v:0', '-show_entries', 'stream=color_transfer:stream_side_data',
         '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=remaining, check=False,
    )
    if result.returncode:
        return None
    streams = json.loads(result.stdout).get('streams') or []
    return hdr_state_from_stream(streams[0] if streams else {})


def _source_groups(item: dict) -> list[list[str]]:
    """Include direct original addresses as well as every advertised rendition."""
    from bot.downloader import _tiktok_variants

    variants = _tiktok_variants(item, original_only=True)
    # Probe HEVC candidates before the many common H.264 SDR renditions. This
    # only orders limited reads; actual transfer/DOVI metadata still decides HDR.
    variants.sort(key=lambda v: not str(v.get('codec') or '').lower().startswith(('h265', 'hevc')))
    groups = [list(v['urls']) for v in variants]
    video = item.get('video') or {}
    for field in ('playAddr', 'downloadAddr', 'PlayAddrStruct'):
        value = video.get(field)
        urls = ([value] if isinstance(value, str) else
                value.get('UrlList') or value.get('urlList') or []
                if isinstance(value, dict) else [])
        urls = [u for u in urls if isinstance(u, str) and u]
        if not urls:
            continue
        existing = next((g for g in groups if set(g).intersection(urls)), None)
        if existing is None:
            groups.append(urls)
        else:
            existing.extend(u for u in urls if u not in existing)
    return groups


def _probe_media_source(url: str, session, deadline: float, budget: list[int],
                        *, reserve_bytes: int = 0) -> bool | None:
    def read(start: int, end: int):
        requested = end - start + 1
        if requested > budget[0]:
            raise ValueError('Source exhausted the total range byte budget')
        # Charge attempts too, keeping retries and mirrors within the same bound.
        budget[0] -= requested
        return _read_range(session, url, start, end, deadline)

    header, total = read(0, _FIRST_BYTES - 1)
    with tempfile.TemporaryDirectory(prefix='tg-tiktok-hdr-probe-') as directory:
        path = Path(directory) / 'source.mp4'
        path.write_bytes(header)
        hdr = _transfer(path, deadline)
        if hdr is not None or total <= len(header):
            return hdr
        tail_size = min(max(0, budget[0] - reserve_bytes), total - len(header))
        if not tail_size:
            return None
        tail_start = total - tail_size
        tail, checked_total = read(tail_start, total - 1)
        if checked_total != total:
            return None
        with path.open('r+b') as target:
            target.truncate(total)
            target.seek(tail_start)
            target.write(tail)
        return _transfer(path, deadline)


def probe_tiktok_item_hdr(item: dict, session, *, timeout_seconds: float = 4.0) -> bool | None:
    """Search every available source/rendition; inspect at most 1 MiB in total.

    A failed probe is unknown, never proof of SDR. Codec and bit depth alone
    cannot establish HDR. One SDR rendition cannot certify an SDR original.
    A bounded tail read supports files with moov at EOF.
    """
    deadline = time.monotonic() + timeout_seconds
    try:
        groups = _source_groups(item)
        if not groups:
            return None
        budget = [_MAX_BYTES]
        all_sdr = True
        for index, urls in enumerate(groups):
            state = None
            for url in dict.fromkeys(urls):
                if budget[0] < _FIRST_BYTES or time.monotonic() >= deadline:
                    break
                try:
                    state = _probe_media_source(
                        url, session, deadline, budget,
                        reserve_bytes=(len(groups) - index - 1) * _FIRST_BYTES,
                    )
                except Exception:
                    continue
                if state is not None:
                    break
            if state is True:
                video = item.setdefault('video', {})
                verified = video.setdefault('_verified_hdr_urls', [])
                if url not in verified:
                    verified.append(url)
                return True
            all_sdr = all_sdr and state is False
        # The normal web ladder often contains only SDR transcodes. Only an
        # explicitly original source plus complete successful probes can prove
        # absence of HDR; otherwise the caller should try the HD resolver.
        original = (item.get('video') or {}).get('encodedType') == 'original'
        return False if original and all_sdr else None
    except Exception:
        # Signed media addresses, cookies, and transport errors stay private.
        return None


def _native_item(session, url: str, deadline: float) -> dict | None:
    from bot.downloader import _TIKTOK_DATA_RE

    try:
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
            return item
        return None
    except Exception:
        return None


def _resolver_item(url: str, expected_id: str | None, deadline: float) -> dict | None:
    """Send only the public post URL to the anonymous HD metadata resolver."""
    session = None
    try:
        from curl_cffi import requests

        from bot.downloader import _tikwm_video_item

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        session = requests.Session(impersonate='firefox133')
        response = session.get('https://www.tikwm.com/api/', params={'url': url, 'hd': '1'},
                               stream=True, allow_redirects=False, timeout=min(3, remaining))
        try:
            if response.status_code != 200:
                return None
            body = bytearray()
            for chunk in response.iter_content(chunk_size=32768):
                if len(body) + len(chunk) > _MAX_BYTES or time.monotonic() >= deadline:
                    return None
                body.extend(chunk)
            payload = json.loads(body)
        finally:
            response.close()
        data = payload.get('data') if payload.get('code') == 0 else None
        if not isinstance(data, dict) or not str(data.get('id') or '').isdigit():
            return None
        path_id = re.search(r'/(?:video|photo)/(\d+)', urlsplit(url).path)
        expected_id = expected_id or (path_id.group(1) if path_id else None)
        if expected_id and str(data['id']) != expected_id:
            return None
        return _tikwm_video_item(data)
    except Exception:
        return None
    finally:
        if session is not None:
            session.close()


def _probe_url(url: str, cookies_file: Path | None, proxy: str | None) -> bool | None:
    deadline = time.monotonic() + 12
    session = None
    try:
        from curl_cffi import requests

        from bot.downloader import _tiktok_cookies
        from bot.proxy import proxy_for

        _validate_url(url)
        session = requests.Session(impersonate='firefox133', proxy=proxy or proxy_for('tiktok'))
        session.cookies.update(_tiktok_cookies(cookies_file))
        item = _native_item(session, url, deadline)
        if item is not None:
            state = probe_tiktok_item_hdr(item, session,
                                          timeout_seconds=min(4, deadline - time.monotonic()))
            if state is not None:
                return state
        resolver_item = _resolver_item(url, str(item['id']) if item and item.get('id') else None,
                                       deadline)
        if resolver_item is None:
            return None
        # Cookies never travel to the resolver, nor to its media addresses.
        session.close()
        session = requests.Session(impersonate='firefox133', proxy=proxy or proxy_for('tiktok'))
        session._tiktok_hdr_anonymous_resolver = True
        return probe_tiktok_item_hdr(resolver_item, session,
                                     timeout_seconds=min(4, deadline - time.monotonic()))
    except Exception:
        return None
    finally:
        if session is not None:
            session.close()


async def probe_tiktok_source_hdr(url: str, cookies_file: Path | None = None,
                                  proxy: str | None = None) -> bool | None:
    """Optional lookup for a cached SDR preview whose source metadata is unknown."""
    return await asyncio.to_thread(_probe_url, url, cookies_file, proxy)
