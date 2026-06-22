"""Admin-tunable runtime settings, cached in memory and persisted in PostgreSQL.

The admin changes these live from the in-chat control panel; they survive
restarts. The total-bandwidth value is also written to a file that a host-side
systemd timer reads, so the LXC's `tc` cap can be changed without redeploying.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bot.db import Database

logger = logging.getLogger(__name__)

# Preset options shown as buttons (0 = unlimited where applicable).
UPLOAD_OPTIONS_MB = (50, 500, 1000, 2000)
SPEED_OPTIONS_MBIT = (0, 25, 100, 300)
BANDWIDTH_OPTIONS_MBIT = (50, 100, 200, 0)

# Hard caps used to validate custom values; the network maxima come from .env.
SIZE_MIN_MB = 10
SIZE_MAX_MB = 2000  # Telegram's hard per-file ceiling
SPEED_MIN_MBIT = 10
MAIN_MAX_MBIT = int(os.getenv("MAIN_MAX_MBIT", "1000"))   # main uplink ceiling
VPN_MAX_MBIT = int(os.getenv("VPN_MAX_MBIT", "1000"))     # VLESS node ceiling

_DEFAULTS: dict[str, int] = {
    "user_upload_mb": 2000,
    "admin_upload_mb": 2000,
    "user_speed_mbit": 0,    # 0 = unlimited (per-download yt-dlp ratelimit)
    "admin_speed_mbit": 0,
    # Extra caps applied only to proxied (VLESS) downloads, per audience.
    "vpn_user_speed_mbit": 0,
    "vpn_admin_speed_mbit": 0,
    "solo_mode": 0,
    "bandwidth_mbit": 100,
}

# Validation range per key (min, max). solo_mode is a 0/1 toggle.
RANGES: dict[str, tuple[int, int]] = {
    "user_upload_mb": (SIZE_MIN_MB, SIZE_MAX_MB),
    "admin_upload_mb": (SIZE_MIN_MB, SIZE_MAX_MB),
    "user_speed_mbit": (0, MAIN_MAX_MBIT),
    "admin_speed_mbit": (0, MAIN_MAX_MBIT),
    "vpn_user_speed_mbit": (0, VPN_MAX_MBIT),
    "vpn_admin_speed_mbit": (0, VPN_MAX_MBIT),
    "bandwidth_mbit": (0, MAIN_MAX_MBIT),
}

# Written for the host bandwidth timer; lives in the shared ./data volume.
_BANDWIDTH_FILE = Path(os.getenv("BANDWIDTH_FILE", "data/bandwidth_mbit.txt"))


class RuntimeConfig:
    def __init__(self) -> None:
        self._values: dict[str, int] = dict(_DEFAULTS)
        self._db: Database | None = None

    async def load(self, db: Database) -> None:
        self._db = db
        stored = await db.fetch_settings()
        for key in _DEFAULTS:
            if key in stored:
                try:
                    self._values[key] = int(stored[key])
                except ValueError:
                    pass
        self._write_bandwidth_file()

    async def set(self, key: str, value: int) -> None:
        if key not in _DEFAULTS:
            raise KeyError(key)
        self._values[key] = value
        if self._db is not None:
            await self._db.set_setting(key, str(value))
        if key == "bandwidth_mbit":
            self._write_bandwidth_file()

    def get(self, key: str) -> int:
        return self._values[key]

    def upload_bytes(self, is_admin: bool) -> int:
        mb = self._values["admin_upload_mb" if is_admin else "user_upload_mb"]
        return mb * 1024 * 1024

    @property
    def user_speed_mbit(self) -> int:
        return self._values["user_speed_mbit"]

    def ratelimit_bps(self, is_admin: bool, via_proxy: bool) -> int | None:
        """Download speed cap (bytes/s) for this download, or None for unlimited.

        Combines the per-audience cap with the separate VLESS-channel cap, taking
        the tighter of the two when the download is routed through the proxy.
        """
        vpn_key = "vpn_admin_speed_mbit" if is_admin else "vpn_user_speed_mbit"
        mbits = [v for v in (
            self._values["admin_speed_mbit" if is_admin else "user_speed_mbit"],
            self._values[vpn_key] if via_proxy else 0,
        ) if v > 0]
        if not mbits:
            return None
        return min(mbits) * 1_000_000 // 8

    @property
    def solo_mode(self) -> bool:
        return bool(self._values["solo_mode"])

    def _write_bandwidth_file(self) -> None:
        try:
            _BANDWIDTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            _BANDWIDTH_FILE.write_text(str(self._values["bandwidth_mbit"]), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write bandwidth file: %s", exc)


# Module-level singleton.
config = RuntimeConfig()
