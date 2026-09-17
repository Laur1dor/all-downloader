"""Read an Instagram post without an account.

Every other route into Instagram runs on a logged-in session, and a session is
the thing that will not stay still: it is tied to a browser somewhere, it is
invalidated on its own schedule, and when it dies the bot does not say so — it
says the post is not there. Measured here: the sessions behind this bot went
stale on 27 August, carousels stopped arriving on 12 September, and nobody could
tell from the outside that those were the same fact.

The embed page is different in kind. It is the page Instagram serves to a third
party putting a post on their own website, so being fetched by a stranger with
no account is the thing it is for. It carries the same GraphQL payload the app
uses — every item of a carousel, every resolution, the caption and the author —
and it needs no cookie at all.

Measured 18 Sep 2026: a two-photo carousel and a video both came back complete
with no session, and their media fetched at full size.

What it does not do is private accounts, and it can be changed by Instagram
without notice. So it is tried first and the old session-based path stays behind
it as the fallback, rather than being thrown away.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# /p/, /reel/, /reels/ and /tv/ all address the same object, and all of them are
# readable through /p/<code>/embed/, so the shape of the link does not matter
# beyond the code in it.
_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)
_CONTEXT_RE = re.compile(r'"contextJSON"\s*:\s*("(?:[^"\\]|\\.)*")')
_EMBED_URL = "https://www.instagram.com/p/{}/embed/captioned/"
# The CDN serves a file to the site it belongs to and refuses it elsewhere.
REFERER = "https://www.instagram.com/"
# The same list the rest of the bot uses for sites that judge by TLS fingerprint.
_IMPERSONATE = ("chrome131", "firefox133", "safari17_0", "chrome124")
_TIMEOUT = 25


@dataclass
class InstagramItem:
    url: str
    is_video: bool


@dataclass
class InstagramPost:
    items: list[InstagramItem] = field(default_factory=list)
    caption: str | None = None
    owner: str | None = None

    def __bool__(self) -> bool:
        return bool(self.items)


def shortcode(url: str) -> str | None:
    found = _SHORTCODE_RE.search(url)
    return found.group(1) if found else None


def _best_image(node: dict) -> str | None:
    """The largest resolution offered, rather than the one meant for a feed."""
    resources = node.get("display_resources") or []
    if resources:
        best = max(resources, key=lambda r: int(r.get("config_width") or 0))
        if best.get("src"):
            return best["src"]
    return node.get("display_url")


def _item_of(node: dict) -> InstagramItem | None:
    if node.get("is_video") and node.get("video_url"):
        return InstagramItem(node["video_url"], True)
    image = _best_image(node)
    return InstagramItem(image, False) if image else None


def _parse(body: str) -> InstagramPost:
    found = _CONTEXT_RE.search(body)
    if not found:
        return InstagramPost()
    try:
        # The payload is a JSON string inside the JSON of the page, so it is
        # decoded twice on purpose.
        context = json.loads(json.loads(found.group(1)))
    except ValueError:
        return InstagramPost()

    post = (context.get("gql_data") or {}).get("shortcode_media") or {}
    if not post:
        return InstagramPost()

    children = (post.get("edge_sidecar_to_children") or {}).get("edges") or []
    nodes = [edge.get("node") or {} for edge in children] if children else [post]
    items = [item for item in (_item_of(node) for node in nodes) if item]

    captions = (post.get("edge_media_to_caption") or {}).get("edges") or []
    caption = None
    if captions:
        caption = ((captions[0].get("node") or {}).get("text") or "").strip() or None

    return InstagramPost(
        items=items,
        caption=caption,
        owner=(post.get("owner") or {}).get("username"),
    )


def read_post(url: str, proxy: str | None = None):
    """The post's media, or an empty result. Returns (post, session).

    The session comes back because the media hosts expect the same client that
    asked for the page; handing the URLs to a fresh connection is how a download
    that looked fine turns into a refusal.
    """
    from curl_cffi import requests as cffi_requests

    code = shortcode(url)
    if not code:
        return InstagramPost(), None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    for target in _IMPERSONATE:
        session = cffi_requests.Session(
            impersonate=target, proxies=proxies, timeout=_TIMEOUT
        )
        try:
            body = session.get(_EMBED_URL.format(code)).text
        except Exception as exc:
            logger.debug("Instagram embed via %s failed: %s", target, exc)
            session.close()
            continue
        post = _parse(body)
        if post:
            return post, session
        session.close()
        # An embed that comes back without a payload means a private account or
        # a removed post far more often than a refused fingerprint, but trying
        # the next one costs a single request.
    return InstagramPost(), None
