"""Adaptive per-platform routing: go direct while it works, fall back to VLESS.

The OpenWRT gateway normally bypasses DPI, so direct is the fastest path. When
DPI is updated and a platform stops being reachable directly, that platform's
traffic is routed through the xray (VLESS) SOCKS proxy instead. A background
loop keeps re-probing, so routing follows the current network reality and
recovers on its own. A platform simply being down (not blocked) never breaks
the logic — it is retried, never cached as a permanent failure.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Stable reachability probes per platform (root endpoints, not specific media).
_PROBE_URLS = {
    "youtube": "https://www.youtube.com/generate_204",
    "tiktok": "https://www.tiktok.com/",
    "instagram": "https://www.instagram.com/",
    "pornhub": "https://www.pornhub.com/",
    "rule34video": "https://rule34video.com/",
    "rule34": "https://rule34.xxx/",
    "joidb": "https://www.the-joi-database.com/",
    "other": "https://www.google.com/generate_204",
}
_PROXY_TEST_URL = "https://www.google.com/generate_204"
_CHECK_INTERVAL = 120.0
_PROBE_TIMEOUT = 8.0

# Platforms that must ALWAYS use the proxy regardless of the direct probe:
# TikTok IP-bans Russian addresses; rule34.xxx serves a Cloudflare anti-bot
# challenge to the server's IP but lets the VLESS node through.
_ALWAYS_PROXY = ("tiktok", "rule34")


class ProxyRouter:
    """Decides, per platform, whether to use the proxy or go direct."""

    def __init__(self, proxy_url: str | None) -> None:
        self._proxy_url = proxy_url or None
        # Optimistic start: assume the gateway's DPI bypass works.
        self._direct_ok: dict[str, bool] = dict.fromkeys(_PROBE_URLS, True)
        self._proxy_ok = False
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._proxy_url is not None

    def proxy_for(self, platform: str) -> str | None:
        """Proxy URL to use for this platform, or None to go direct."""
        if not self.enabled:
            return None
        if platform in _ALWAYS_PROXY:
            return self._proxy_url  # never usable directly (e.g. TikTok in RU)
        if self._direct_ok.get(platform, True):
            return None  # direct works (and is faster) — prefer it
        if self._proxy_ok:
            return self._proxy_url  # blocked directly, route through VLESS
        return None  # nothing works — try direct and let it fail naturally

    async def start(self) -> None:
        if self.enabled:
            await self._check_all()  # decide routing before the first download
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_CHECK_INTERVAL)
            try:
                await self._check_all()
            except Exception:  # never let the monitor die
                logger.exception("Proxy health check failed")

    async def _check_all(self) -> None:
        self._proxy_ok = await self._reachable(_PROXY_TEST_URL, self._proxy_url)
        for platform, url in _PROBE_URLS.items():
            self._direct_ok[platform] = await self._reachable(url, None)
        blocked = [p for p, ok in self._direct_ok.items() if not ok]
        if blocked:
            logger.info(
                "Direct blocked for [%s]; VLESS proxy %s",
                ", ".join(blocked),
                "available" if self._proxy_ok else "DOWN",
            )

    @staticmethod
    async def _reachable(url: str, proxy: str | None) -> bool:
        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, proxy=proxy, allow_redirects=False) as response,
            ):
                return response.status < 500
        except (aiohttp.ClientError, TimeoutError, OSError):
            return False


# Module-level singleton, configured at startup and queried by the downloader.
_router = ProxyRouter(None)


def configure_router(proxy_url: str | None) -> ProxyRouter:
    global _router
    _router = ProxyRouter(proxy_url)
    return _router


def proxy_for(platform: str) -> str | None:
    return _router.proxy_for(platform)


def forced_proxy() -> str | None:
    """The configured proxy URL regardless of routing — for content-level retries
    (e.g. a platform reachable directly but blocking this server's IP per-post)."""
    return _router._proxy_url
