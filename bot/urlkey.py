"""Canonical cache keys for links that point at the same post.

The file_id cache used to be keyed on the raw link text, which meant it almost
never hit. TikTok hands out a fresh short link every time a post is shared, and
the address it expands to carries a per-copy `_t` parameter, so the same video
arrived under a new key on every send and was downloaded again from scratch.
Five spellings of one video produced five keys.

The key is therefore built from what identifies the post — the platform and its
id — and falls back to the address with the noise stripped when no id is known.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Hosts that hand out per-share links; these must be expanded before a key can
# be built, since the id is not in the address at all.
SHORTLINK_HOSTS = (
    "vt.tiktok.com", "vm.tiktok.com", "t.tiktok.com",
    "youtu.be", "pin.it", "on.soundcloud.com",
)

_PATH_ID_PATTERNS = {
    "tiktok": re.compile(r"/(?:video|photo)/(\d+)"),
    "instagram": re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"),
    "twitter": re.compile(r"/status/(\d+)"),
    "youtube": re.compile(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})"),
}
_QUERY_ID_KEYS = {
    "youtube": "v",
    "pornhub": "viewkey",
    "rule34": "id",
    "rule34video": "id",
}


def _platform(host: str) -> str:
    host = host.lower().removeprefix("www.").removeprefix("m.")
    table = {
        "tiktok.com": "tiktok", "instagram.com": "instagram", "instagr.am": "instagram",
        "x.com": "twitter", "twitter.com": "twitter",
        "youtube.com": "youtube", "youtu.be": "youtube",
        "pornhub.com": "pornhub", "pornhub.org": "pornhub",
        "rule34.xxx": "rule34", "rule34video.com": "rule34video",
        "the-joi-database.com": "joidb", "soundcloud.com": "soundcloud",
        "open.spotify.com": "spotify", "spotify.com": "spotify",
    }
    for domain, name in table.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def canonical_key(url: str) -> str:
    """The string to hash for the cache: one post, one key.

    Short links must be expanded first — their id lives behind a redirect, not
    in the address — otherwise the address itself is normalised as a fallback.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    platform = _platform(host)
    path = parsed.path

    if platform == "youtube" and host.endswith("youtu.be"):
        video = path.strip("/").split("/")[0]
        if video:
            return f"youtube:{video}"

    pattern = _PATH_ID_PATTERNS.get(platform)
    if pattern:
        found = pattern.search(path)
        if found:
            return f"{platform}:{found.group(1)}"

    key = _QUERY_ID_KEYS.get(platform)
    if key:
        values = parse_qs(parsed.query).get(key)
        if values and values[0]:
            return f"{platform}:{values[0]}"

    # Nothing identifying was recognised: keep the address but drop the parts
    # that vary between shares of the same page. Only the host is lowercased —
    # paths are case-sensitive, and folding them would merge distinct pages.
    host = host.removeprefix("www.").removeprefix("m.")
    return f"{host}{path.rstrip('/')}"


def is_short_link(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in SHORTLINK_HOSTS)
