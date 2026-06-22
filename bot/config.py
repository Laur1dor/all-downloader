"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

DEFAULT_HEALTH_PORT = 30080
DEFAULT_COOKIES_FILE = "data/cookies.txt"
DEFAULT_LEGACY_DUMP_FILE = "info.txt"


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_id: int
    database_dsn: str
    health_port: int
    cookies_file: Path | None
    legacy_dump_file: Path | None
    # Self-hosted telegram-bot-api server (raises the upload limit to 2 GB).
    api_base_url: str | None
    max_upload_bytes: int
    # SOCKS/HTTP proxy (xray/VLESS) used as a fallback when direct is DPI-blocked.
    proxy_url: str | None


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Environment variable {name} is not set")
    return value


def _existing_file(env_name: str, default: str) -> Path | None:
    path = Path(os.getenv(env_name, default))
    return path if path.is_file() else None


def load_settings() -> Settings:
    load_dotenv()

    # BOT_TOKEN is the canonical name; TOKEN is accepted for backwards compatibility.
    bot_token = os.getenv("BOT_TOKEN") or _require("TOKEN")

    try:
        admin_id = int(_require("ADMIN_ID"))
    except ValueError as exc:
        raise ConfigError("ADMIN_ID must be an integer Telegram user id") from exc

    database_dsn = (
        f"postgresql://{quote_plus(_require('DB_USER'))}:{quote_plus(_require('DB_PASSWORD'))}"
        f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
        f"/{_require('DB_NAME')}"
    )

    api_base_url = os.getenv("TELEGRAM_API_URL") or None
    # 50 MB is the cloud Bot API limit; a self-hosted server allows up to 2000.
    default_limit = "2000" if api_base_url else "50"
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", default_limit))

    return Settings(
        bot_token=bot_token,
        admin_id=admin_id,
        database_dsn=database_dsn,
        health_port=int(os.getenv("HEALTH_PORT", str(DEFAULT_HEALTH_PORT))),
        cookies_file=_existing_file("COOKIES_FILE", DEFAULT_COOKIES_FILE),
        legacy_dump_file=_existing_file("LEGACY_DUMP_FILE", DEFAULT_LEGACY_DUMP_FILE),
        api_base_url=api_base_url,
        max_upload_bytes=max_upload_mb * 1024 * 1024,
        proxy_url=os.getenv("PROXY_URL") or None,
    )
