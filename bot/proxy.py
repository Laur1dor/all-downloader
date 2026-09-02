"""Config-driven per-platform routing with a health-checked pool cascade.

Each platform is routed by a policy from ``data/routing.toml`` (hot-reloaded, no
restart needed):

  direct       — always straight out
  goida        — via the free public pool (socks :2079), then bypass, then main
  bypass       — via the curated bypass pool (socks :2078), then goida, then main
  main         — reliable cascade: goida → bypass → main (personal node last)
  adaptive     — direct while it works, otherwise the cascade above
  socks5h://.. — a dedicated external proxy pinned for this platform (e.g. WARP
                 for DDoS-Guard sites that need one stable Cloudflare exit IP)
  vless://...  — a node pinned for this platform in xray by domain-routing
                 (routed via the main socks; xray sends it to that node)

Pool cascade & the personal node
--------------------------------
The operator's personal VLESS node (``main`` / socks :2080) is treated as a
**last resort**: every cascade puts it last, so normal traffic rides the free
``goida`` pool and the curated ``bypass`` pool and only touches the personal
node when *both* of those are down. This keeps the personal subscription
unloaded during day-to-day use while still guaranteeing an exit exists.

Health checks are multi-level:
  * xray's observatory drops individual dead nodes *inside* each pool;
  * this router probes each *pool* every ``_CHECK_INTERVAL`` seconds and skips a
    whole pool that is down, cascading to the next live one.
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
# A block answers as confidently as a working site. rule34 hands the server's
# address a 403 challenge page while the same request through a proxy returns
# 200, and "status < 500" read that 403 as "direct works", so the platform was
# routed direct and every download failed. These mean blocked, not reachable.
_BLOCKED_STATUSES = frozenset({401, 403, 451})

# Preferred pool order per policy. "main" (the personal node) is ALWAYS last so
# day-to-day traffic never loads the operator's personal subscription while a
# free/bypass pool is alive.
_CASCADES: dict[str, tuple[str, ...]] = {
    "goida": ("goida", "bypass", "main"),
    "bypass": ("bypass", "goida", "main"),
    "main": ("goida", "bypass", "main"),
    "adaptive": ("goida", "bypass", "main"),
}

# The personal exit can be carried by any of three engines: xray speaks
# vless/vmess/trojan/ss, the AmneziaWG container carries an obfuscated
# WireGuard tunnel, and sing-box carries hysteria2/tuic, which xray does not
# speak at all. Which one is live is the admin's choice in /control, made after
# sending the config to /vpn — so the address is resolved per call rather than
# fixed at startup.
_ENGINE_PROXIES = {
    "xray": None,  # filled from PROXY_URL at configure time
    "awg": os.getenv("AWG_PROXY_URL", "socks5h://awg:1080"),
    "singbox": os.getenv("SINGBOX_PROXY_URL", "socks5h://singbox:2081"),
}

_ROUTING_FILE = Path(os.getenv("ROUTING_FILE", "data/routing.toml"))
# Used when routing.toml is missing/unreadable — matches the old hardcoded policy.
_DEFAULT_ROUTING: dict = {
    "defaults": {"policy": "adaptive", "main_fallback": "goida"},
    "services": {"tiktok": "main", "rule34": "goida", "rule34video": "bypass"},
}


class ProxyRouter:
    """Decides, per platform, whether to go direct or through which pool."""

    def __init__(
        self, proxy_url: str | None, goida_url: str | None = None,
        bypass_url: str | None = None,
    ) -> None:
        self._proxy_url = proxy_url or None
        self._goida_url = goida_url or None
        self._bypass_url = bypass_url or None
        # Optimistic start: assume the gateway's DPI bypass works.
        self._direct_ok: dict[str, bool] = dict.fromkeys(_PROBE_URLS, True)
        # Per-pool health. Start optimistic so the first downloads before the
        # first probe completes still have an exit to try.
        self._pool_ok: dict[str, bool] = {"main": True, "goida": True, "bypass": True}
        self._task: asyncio.Task | None = None
        self._routing: dict = _DEFAULT_ROUTING
        self._routing_mtime: float | None = None
        self._load_routing()

    @property
    def enabled(self) -> bool:
        return any((self._proxy_url, self._goida_url, self._bypass_url))

    def _main_url(self) -> str | None:
        """The personal exit, through the engine the admin selected."""
        engine = _main_engine()
        if engine == "xray":
            return self._proxy_url
        return _ENGINE_PROXIES.get(engine) or self._proxy_url

    def _pool_url(self, name: str) -> str | None:
        return {
            "main": self._main_url(),
            "goida": self._goida_url,
            "bypass": self._bypass_url,
        }.get(name)

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

    def _cascade(self, policy: str) -> str | None:
        """First live pool in the policy's order; personal node is always last.

        If no pool probes healthy, fall back to the last configured pool in the
        order (the personal node) as a last-ditch attempt rather than giving up.
        """
        order = _CASCADES.get(policy, _CASCADES["adaptive"])
        configured = [(n, self._pool_url(n)) for n in order if self._pool_url(n)]
        for name, url in configured:
            if self._pool_ok.get(name):
                return url
        return configured[-1][1] if configured else None

    def proxy_for(self, platform: str) -> str | None:
        """Proxy URL to use for this platform, or None to go direct."""
        if not self.enabled:
            return None
        policy = self._policy_for(platform)
        if platform == "tiktok" and _tiktok_own_vpn() and self._main_url():
            # /control says TikTok goes through the operator's own subscription.
            return self._main_url()
        if policy.startswith(("socks5://", "socks5h://", "http://", "https://")):
            # A dedicated external proxy pinned for this platform (e.g. WARP for
            # DDoS-Guard sites that need one stable Cloudflare exit IP).
            return policy
        if policy.startswith("vless://"):
            # Node pinned in xray by domain-routing → must ride the main socks.
            return self._proxy_url
        if policy == "direct":
            return None
        if policy == "adaptive":
            if self._direct_ok.get(platform, True):
                return None
            return self._cascade("adaptive")
        return self._cascade(policy)

    def forced_proxy(self, platform: str = "") -> str | None:
        """Proxy for a content-level retry (a post that blocks this IP).

        Prefer a *different* live pool than the platform's primary, so a retry
        after a block (e.g. DDoS-Guard 504 on the bypass IPs) lands on a fresh
        exit instead of hammering the same one. Falls back to the primary (whose
        own exit rotates) when no other pool is live.
        """
        if not self.enabled:
            return None
        if platform:
            policy = self._policy_for(platform)
            if policy.startswith(("socks5://", "socks5h://", "http://", "https://")):
                # Dedicated proxy (e.g. WARP) is primary; a retry after a block
                # uses a rotating pool as an alternate exit.
                return self._cascade("goida")
            if policy.startswith("vless://"):
                return self._main_url()
            if policy in _CASCADES:
                primary = self._cascade(policy)
                for name in _CASCADES[policy]:
                    url = self._pool_url(name)
                    if url and self._pool_ok.get(name) and url != primary:
                        return url
                return primary
        return self._cascade("adaptive")

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
        for name in ("main", "goida", "bypass"):
            url = self._pool_url(name)
            self._pool_ok[name] = (
                await self._reachable(_PROXY_TEST_URL, url) if url else False
            )
        for platform, url in _PROBE_URLS.items():
            self._direct_ok[platform] = await self._reachable(url, None)
        blocked = [p for p, ok in self._direct_ok.items() if not ok]
        dead_pools = [n for n in ("goida", "bypass", "main")
                      if self._pool_url(n) and not self._pool_ok[n]]
        if blocked or dead_pools:
            logger.info(
                "Direct blocked for [%s]; pools main=%s goida=%s bypass=%s",
                ", ".join(blocked) or "none",
                "up" if self._pool_ok["main"] else "DOWN",
                "up" if self._pool_ok["goida"] else "DOWN",
                "up" if self._pool_ok["bypass"] else "DOWN",
            )

    @staticmethod
    async def _reachable(url: str, proxy: str | None) -> bool:
        import aiohttp

        # aiohttp's own `proxy=` speaks HTTP(S) only, so every pool (which is a
        # SOCKS endpoint) used to fail the probe and report DOWN regardless of
        # its real state. SOCKS needs a connector instead.
        try:
            connector = None
            http_proxy = proxy
            if proxy and proxy.startswith(("socks4://", "socks5://", "socks5h://")):
                from aiohttp_socks import ProxyConnector

                # aiohttp_socks knows no "socks5h" scheme; the trailing h only
                # means "resolve the hostname at the proxy", which is rdns.
                connector = ProxyConnector.from_url(
                    proxy.replace("socks5h://", "socks5://", 1), rdns=True
                )
                http_proxy = None
            timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT)
            async with (
                aiohttp.ClientSession(timeout=timeout, connector=connector) as session,
                session.get(url, proxy=http_proxy, allow_redirects=False) as response,
            ):
                return response.status < 500 and response.status not in _BLOCKED_STATUSES
        except Exception:
            # Building the connector can raise too, and a health probe must never
            # propagate: an unreachable pool is a DOWN, not a crashed bot.
            return False


# Module-level singleton, configured at startup and queried by the downloader.
_router = ProxyRouter(None)


def configure_router(
    proxy_url: str | None, goida_url: str | None = None, bypass_url: str | None = None
) -> ProxyRouter:
    global _router
    _ENGINE_PROXIES["xray"] = proxy_url or None
    _router = ProxyRouter(proxy_url, goida_url, bypass_url)
    return _router


def proxy_for(platform: str) -> str | None:
    return _router.proxy_for(platform)


# Ports the TikTok nodes sit on, highest first — see build_config for why each
# node gets its own inbound rather than sharing one behind a balancer.
_TIKTOK_LADDER_PORTS = int(os.getenv("TIKTOK_MAX_INBOUNDS", "8"))
_TIKTOK_BASE_PORT = int(os.getenv("TIKTOK_SOCKS_PORT", "2077"))


def _main_engine() -> str:
    """Engine name from /control; read late so a switch takes effect at once."""
    try:
        from bot.runtime import config

        return config.main_exit
    except Exception:
        return "xray"


def _tiktok_own_vpn() -> bool:
    """Admin's choice from /control, read late so a switch takes effect at once."""
    try:
        from bot.runtime import config

        return config.tiktok_via_own_vpn
    except Exception:
        return False


def proxy_ladder(platform: str) -> list[str]:
    """Exits to try in turn for this platform, first one first.

    TikTok answers an address it has tired of with a stub page, so repeating a
    request through the same exit cannot succeed however many times it is tried.
    Each retry therefore moves to the next node. Everywhere else the single
    proxy is the whole ladder.
    """
    first = _router.proxy_for(platform)
    if platform != "tiktok" or not first:
        return [first] if first else []
    if _tiktok_own_vpn():
        # One node, one rung: there is nothing to rotate through, and falling
        # back to the free pool would quietly undo the admin's choice.
        own = _router._main_url()
        return [own] if own else [first]
    base, _, port_text = first.rpartition(":")
    try:
        port = int(port_text)
    except ValueError:
        return [first]
    # The rungs exist only in front of the xray pool, where each node was given
    # its own inbound. Any other exit — WARP, an AmneziaWG tunnel — is a single
    # proxy, and counting ports down from it would dial ports nothing listens on.
    if port != _TIKTOK_BASE_PORT:
        return [first]
    return [f"{base}:{port - i}" for i in range(_TIKTOK_LADDER_PORTS)]


def forced_proxy(platform: str = "") -> str | None:
    """The proxy URL for a content-level retry, honouring the platform's pool."""
    return _router.forced_proxy(platform)
