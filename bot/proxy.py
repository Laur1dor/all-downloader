"""Config-driven per-platform routing.

Each platform is routed by a policy from ``data/routing.toml`` (hot-reloaded, no
restart needed):

  direct       — always straight out
  main         — always via the main VLESS node (socks :2080)
  goida        — always via the free public pool (socks :2079, random exit)
  adaptive     — direct while it works, main node when blocked
  vless://...  — a node pinned for this platform in xray by domain-routing
                 (the bot routes it via the main socks; xray sends it to that node)

If the main node is down, anything routed through it falls back to the goida pool
(``defaults.main_fallback``), so one dead node never takes the whole bot down.
The goida pool is health-checked continuously by xray's observatory (dead nodes
drop out) and picks a random exit each connection, so banned-site retries land on
different IPs with no stale caching.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import tomllib

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

_ROUTING_FILE = Path(os.getenv("ROUTING_FILE", "data/routing.toml"))
# Used when routing.toml is missing/unreadable — matches the old hardcoded policy.
_DEFAULT_ROUTING: dict = {
    "defaults": {"policy": "adaptive", "main_fallback": "goida"},
    "services": {"tiktok": "main", "rule34": "goida", "rule34video": "goida"},
}


class ProxyRouter:
    """Decides, per platform, whether to go direct or through which pool."""

    def __init__(self, proxy_url: str | None, goida_url: str | None = None) -> None:
        self._proxy_url = proxy_url or None
        self._goida_url = goida_url or None
        # Optimistic start: assume the gateway's DPI bypass works.
        self._direct_ok: dict[str, bool] = dict.fromkeys(_PROBE_URLS, True)
        self._proxy_ok = False
        self._goida_ok = False
        self._task: asyncio.Task | None = None
        self._routing: dict = _DEFAULT_ROUTING
        self._routing_mtime: float | None = None
        self._load_routing()

    @property
    def enabled(self) -> bool:
        return self._proxy_url is not None

    def _load_routing(self) -> None:
        """(Re)load routing.toml when it appears or changes — hot, no restart."""
        try:
            mtime = _ROUTING_FILE.stat().st_mtime
        except OSError:
            return  # no file → keep whatever we have (defaults)
        if mtime == self._routing_mtime:
            return
        try:
            with _ROUTING_FILE.open("rb") as fh:
                self._routing = tomllib.load(fh)
            self._routing_mtime = mtime
            logger.info(
                "Loaded routing.toml (%d service rules)",
                len(self._routing.get("services", {})),
            )
        except (OSError, tomllib.TOMLDecodeError):
            logger.exception("Failed to parse routing.toml; keeping previous routing")

    def _policy_for(self, platform: str) -> str:
        self._load_routing()  # dynamic: pick up edits between calls
        services = self._routing.get("services", {})
        if platform in services:
            return str(services[platform])
        return str(self._routing.get("defaults", {}).get("policy", "adaptive"))

    def _fallback(self) -> str:
        return str(self._routing.get("defaults", {}).get("main_fallback", "goida"))

    def _main_or_fallback(self) -> str | None:
        if self._proxy_ok or not self._goida_url:
            return self._proxy_url
        if self._fallback() == "goida" and self._goida_ok:
            return self._goida_url  # main node down → use the free pool
        return self._proxy_url

    def proxy_for(self, platform: str) -> str | None:
        """Proxy URL to use for this platform, or None to go direct."""
        if not self.enabled:
            return None
        policy = self._policy_for(platform)
        if policy.startswith("vless://"):
            policy = "main"  # node pinned in xray by domain-routing; go via main
        if policy == "direct":
            return None
        if policy == "goida":
            return self._goida_url or self._proxy_url
        if policy == "main":
            return self._main_or_fallback()
        # adaptive: direct while it works, otherwise the main node (or fallback)
        if self._direct_ok.get(platform, True):
            return None
        return self._main_or_fallback()

    def forced_proxy(self, platform: str = "") -> str | None:
        """Proxy for a content-level retry (a post that blocks this IP). Honours
        the platform's pool so a goida-routed site retries via a new random exit."""
        if platform and self._policy_for(platform) == "goida":
            return self._goida_url or self._proxy_url
        return self._proxy_url

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
        if self._goida_url:
            self._goida_ok = await self._reachable(_PROXY_TEST_URL, self._goida_url)
        for platform, url in _PROBE_URLS.items():
            self._direct_ok[platform] = await self._reachable(url, None)
        blocked = [p for p, ok in self._direct_ok.items() if not ok]
        if blocked or not self._proxy_ok:
            logger.info(
                "Direct blocked for [%s]; main node %s, goida pool %s",
                ", ".join(blocked) or "none",
                "up" if self._proxy_ok else "DOWN",
                "up" if self._goida_ok else "down/disabled",
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


def configure_router(proxy_url: str | None, goida_url: str | None = None) -> ProxyRouter:
    global _router
    _router = ProxyRouter(proxy_url, goida_url)
    return _router


def proxy_for(platform: str) -> str | None:
    return _router.proxy_for(platform)


def forced_proxy(platform: str = "") -> str | None:
    """The proxy URL for a content-level retry, honouring the platform's pool."""
    return _router.forced_proxy(platform)
