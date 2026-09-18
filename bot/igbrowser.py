"""Ask the browser container what a post's media actually is.

Both of the bot's own roads into Instagram can be shut at the same time, and on
18 Sep they were. The embed page needs no account but Instagram now serves it
with a null payload for most posts - one of six, measured in an afternoon. The
session path covers the rest until the account is restricted, and then every API
call comes back as the home page instead of JSON, which empties gallery-dl and
yt-dlp together.

The browser is a third road, and it was open while both of those were shut: the
post page renders, and it carries the media in its own script tags. The work of
reading that happens in the container that holds the browser (scripts/igresolve.py);
this is only the asking, done the same way a login is asked for - a file appears,
an answer appears beside it. The bot does not get the docker socket for it.

It is deliberately last. It costs a page load and a Chromium, where the embed
costs one request, so it earns its place only when the cheaper roads have
already returned nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REQUEST_FILE = Path(os.getenv("IG_RESOLVE_REQUEST", "data/igresolve_request"))
RESULT_FILE = Path(os.getenv("IG_RESOLVE_RESULT", "data/igresolve_result.json"))
# A page load plus a browser start. Generous, because the alternative to waiting
# is telling somebody the post does not exist.
TIMEOUT = int(os.getenv("IG_RESOLVE_TIMEOUT", "90"))
_POLL = 1.5
ENABLED = os.getenv("IG_RESOLVE", "1") not in ("0", "false", "no")

# One at a time. Each resolve starts a Chromium against the same browser
# profile, and two of those at once would fight over it.
_lock = asyncio.Lock()


@dataclass
class ResolvedPost:
    items: list[tuple[str, bool]]  # (url, is_video), in post order
    owner: str | None = None
    caption: str | None = None


async def resolve(url: str) -> ResolvedPost | None:
    """The post's media, or None when the browser could not produce it."""
    if not ENABLED:
        return None

    async with _lock:
        try:
            if RESULT_FILE.exists():
                RESULT_FILE.unlink()
        except OSError:
            pass
        try:
            REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
            REQUEST_FILE.write_text(url, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not ask the browser to resolve: %s", exc)
            return None

        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL)
            if not RESULT_FILE.exists():
                continue
            try:
                payload = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Being written as we read it; look again next time round.
                continue

            if not payload.get("ok"):
                logger.info(
                    "Browser could not resolve %s: %s",
                    url, payload.get("error", "no reason given"),
                )
                return None

            items = [
                (item["url"], bool(item.get("is_video")))
                for item in payload.get("items") or []
                if item.get("url")
            ]
            if not items:
                return None
            logger.info(
                "Browser resolved %s to %d item(s) in %ss",
                url, len(items), payload.get("seconds", "?"),
            )
            return ResolvedPost(
                items=items,
                owner=payload.get("owner"),
                caption=payload.get("caption"),
            )

        logger.info(
            "Browser did not answer for %s within %ss - is the iglogin "
            "container running?", url, TIMEOUT,
        )
        return None
