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
import threading
import time
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


# What the embed page turned out to be holding. There is no third value on
# purpose: see the note above _parse for why this cannot report a deletion.
SERVED = "served"        # the payload was there
WITHHELD = "withheld"    # it was not, and the reason is not knowable here


@dataclass
class InstagramPost:
    items: list[InstagramItem] = field(default_factory=list)
    caption: str | None = None
    owner: str | None = None
    state: str = WITHHELD
    # Set only when Instagram itself said why it withheld the post - never
    # inferred from an empty answer. See _gate_of.
    gate: str | None = None

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


class WithheldMediaError(Exception):
    """The post is a video, and the embed did not give its URL."""


def _item_of(node: dict) -> InstagramItem | None:
    if node.get("is_video"):
        if node.get("video_url"):
            return InstagramItem(node["video_url"], True)
        # Measured on a real reel: __typename GraphVideo, is_video true,
        # video_duration and view count all present — and no video_url. Falling
        # back to display_url here would hand back the cover frame as if it were
        # the video, which looks like success and is not. The post is simply not
        # servable this way, so the session path gets it.
        raise WithheldMediaError(node.get("shortcode") or "video")
    image = _best_image(node)
    return InstagramItem(image, False) if image else None


# An empty embed does not say why it is empty, and a guess here is expensive.
#
# This used to read the page for a rendered image and call its absence proof the
# post had been deleted. Measured against five posts of known state, that is
# simply untrue: of three live posts one rendered nothing and was declared gone,
# while the two genuinely deleted ones looked the same as it. The post page and
# the cookie-fed embed do not separate them either. The one witness that does is
# the session path, which fetched both live posts and returned nothing for the
# deleted one — so that verdict belongs downstream, and this reports only whether
# it was served.


def _parse(body: str) -> InstagramPost:
    found = _CONTEXT_RE.search(body)
    if not found:
        return InstagramPost(state=WITHHELD)
    try:
        # The payload is a JSON string inside the JSON of the page, so it is
        # decoded twice on purpose.
        context = json.loads(json.loads(found.group(1)))
    except ValueError:
        return InstagramPost(state=WITHHELD)

    post = (context.get("gql_data") or {}).get("shortcode_media") or {}
    if not post:
        return InstagramPost(state=WITHHELD)

    children = (post.get("edge_sidecar_to_children") or {}).get("edges") or []
    nodes = [edge.get("node") or {} for edge in children] if children else [post]
    try:
        items = [item for item in (_item_of(node) for node in nodes) if item]
    except WithheldMediaError as exc:
        # All or nothing: half a carousel is not the post either.
        logger.info("Instagram withheld the video for %s", exc)
        return InstagramPost(state=WITHHELD)

    captions = (post.get("edge_media_to_caption") or {}).get("edges") or []
    caption = None
    if captions:
        caption = ((captions[0].get("node") or {}).get("text") or "").strip() or None

    return InstagramPost(
        items=items,
        caption=caption,
        owner=(post.get("owner") or {}).get("username"),
        state=SERVED if items else WITHHELD,
    )


# --- the logged-out GraphQL read ------------------------------------------------
#
# The road that needs no account and still carries most posts.
#
# By 23 Sep every other road was shut or thinning. The account met a CAPTCHA on
# every page, so the browser and the session saw nothing - and a CAPTCHA is for a
# person, not for code. The embed answered with a null payload for most posts.
# What the logged-out web app does instead is ask /api/graphql for the post by
# media id, and it is given the post in Instagram's v1 shape. yt-dlp uses the same
# call, but only after trying the account first whenever it has cookies - which,
# with a restricted account, fails before it gets there - and it discards photo
# posts entirely.
#
# Measured on eleven posts: seven of ten live posts served in under a second,
# including three of the four that had just failed in the bot, reels as video
# and carousels whole; the deleted control came back empty. The two it did not
# serve carry a gating_ruling - hidden from logged-out visitors - and fall
# through to the roads behind it.
#
# Candidate order was measured rather than assumed: the first image candidate was
# the largest on all four posts checked and equal to the original size, up to
# 3072x4096, and the three video types were identical. Width wins when it is
# given; the first entry otherwise.
_GRAPHQL_URL = "https://www.instagram.com/api/graphql"
_GRAPHQL_DOC = "27130156389949648"
_GRAPHQL_NAME = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
_APP_ID = "936619743392459"
_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_LSD_RE = re.compile(r'"LSD",\[\],\{"token":"([^"]+)"')
# The token comes from the home page, which is half a megabyte. Fetching it for
# every post would double the anonymous requests - and anonymous access is rate
# limited per address - so it is kept for a while and refetched when refused.
_TOKEN_TTL = 1200

_token_lock = threading.Lock()
_token: dict = {"lsd": None, "cookies": {}, "at": 0.0, "proxy": object()}


def _media_id(code: str) -> str:
    """The numeric id a shortcode stands for. Longer codes are private posts,
    whose first eleven characters are the public part."""
    value = 0
    for char in code[:11]:
        value = value * 64 + _ALPHABET.index(char)
    return str(value)


def _largest(candidates) -> str | None:
    usable = [c for c in candidates or [] if isinstance(c, dict) and c.get("url")]
    if not usable:
        return None
    if any(c.get("width") for c in usable):
        return max(usable, key=lambda c: c.get("width") or 0)["url"]
    return usable[0]["url"]


def _graphql_items(product: dict) -> list[InstagramItem]:
    nodes = product.get("carousel_media") or [product]
    items: list[InstagramItem] = []
    for node in nodes:
        if node.get("video_versions") or node.get("media_type") == 2:
            url = _largest(node.get("video_versions"))
            if not url:
                # Never the cover frame in place of the video.
                raise WithheldMediaError(product.get("code") or "video")
            items.append(InstagramItem(url, True))
            continue
        url = _largest((node.get("image_versions2") or {}).get("candidates"))
        if url:
            items.append(InstagramItem(url, False))
    return items


AGE_GATE = "age"


def _gate_of(media: dict) -> str | None:
    """Instagram's own reason for withholding the post, when it gives one.

    Age-restricted posts come back with a gating_ruling that says so in words -
    measured on both such posts to hand: gating_type 3, title "Age-restricted
    content". Nothing anonymous can serve those: the embed answered null and the
    browser needed the signed-in account. So this is the one withheld answer
    that is allowed to change the route, and only because Instagram stated it
    rather than because something came back empty.
    """
    ruling = media.get("gating_ruling")
    if not isinstance(ruling, dict):
        return None
    words = " ".join(
        str(ruling.get(key) or "") for key in ("title", "description")
    ).lower()
    return AGE_GATE if "age-restricted" in words else None


def _parse_graphql(body: str) -> InstagramPost:
    try:
        data = json.loads(body)
    except ValueError:
        return InstagramPost(state=WITHHELD)
    media = ((data.get("data") or {}).get("xig_polaris_media")) or {}
    product = media.get("if_not_gated_logged_out")
    if not isinstance(product, dict):
        # Gated for logged-out visitors, removed, or refused. Only the first,
        # and only when Instagram names it, is recorded; the rest go on to the
        # next road as they always did.
        return InstagramPost(state=WITHHELD, gate=_gate_of(media))
    try:
        items = _graphql_items(product)
    except WithheldMediaError as exc:
        logger.info("Instagram withheld the video for %s", exc)
        return InstagramPost(state=WITHHELD)
    caption = product.get("caption") or {}
    return InstagramPost(
        items=items,
        caption=((caption.get("text") or "").strip() or None)
        if isinstance(caption, dict) else None,
        owner=(product.get("user") or {}).get("username"),
        state=SERVED if items else WITHHELD,
    )


def _logged_out_token(session, proxy, refresh: bool = False):
    """(lsd, cookies) for a logged-out client, reused while it is fresh."""
    with _token_lock:
        fresh = (
            not refresh
            and _token["lsd"]
            and _token["proxy"] == proxy
            and time.monotonic() - _token["at"] < _TOKEN_TTL
        )
        if fresh:
            return _token["lsd"], dict(_token["cookies"])
    home = session.get("https://www.instagram.com/").text
    found = _LSD_RE.search(home)
    if not found:
        return None, {}
    cookies = {name: value for name, value in session.cookies.items()}
    with _token_lock:
        _token.update(lsd=found.group(1), cookies=cookies,
                      at=time.monotonic(), proxy=proxy)
    return found.group(1), cookies


def read_post_graphql(url: str, proxy: str | None = None):
    """The post as the logged-out web app is given it. Returns (post, session)."""
    from curl_cffi import requests as cffi_requests

    code = shortcode(url)
    if not code:
        return InstagramPost(), None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = cffi_requests.Session(
        impersonate="chrome131", proxies=proxies, timeout=_TIMEOUT
    )
    try:
        for refresh in (False, True):
            lsd, cookies = _logged_out_token(session, proxy, refresh=refresh)
            if not lsd:
                break
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=".instagram.com")
            response = session.post(
                _GRAPHQL_URL,
                headers={
                    "X-IG-App-ID": _APP_ID,
                    "X-FB-LSD": lsd,
                    "X-CSRFToken": cookies.get("csrftoken", ""),
                    "X-FB-Friendly-Name": _GRAPHQL_NAME,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"https://www.instagram.com/p/{code}/",
                },
                data={
                    "lsd": lsd,
                    "fb_api_caller_class": "RelayModern",
                    "fb_api_req_friendly_name": _GRAPHQL_NAME,
                    "server_timestamps": "true",
                    "variables": json.dumps(
                        {"media_id": _media_id(code)}, separators=(",", ":")
                    ),
                    "doc_id": _GRAPHQL_DOC,
                },
            )
            if response.status_code == 200 and response.text.lstrip()[:1] == "{":
                post = _parse_graphql(response.text)
                if post:
                    return post, session
                session.close()
                return post, None
            # A stale token is answered with something that is not the JSON;
            # one refetch, then this road has had its turn.
            logger.debug("Instagram GraphQL answered HTTP %s", response.status_code)
    except Exception as exc:
        logger.debug("Instagram GraphQL read failed: %s", exc)
    session.close()
    return InstagramPost(), None


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

    # The logged-out GraphQL read carries most posts; the embed is kept behind
    # it for the ones it does not.
    post, session = read_post_graphql(url, proxy)
    if post and session is not None:
        return post, session
    if post.gate == AGE_GATE:
        # The embed is anonymous too; it answered null for these. Skip it.
        return post, None

    last = InstagramPost()

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
        last = post
        session.close()
        # An embed that comes back without a payload means a private account or
        # a removed post far more often than a refused fingerprint, but trying
        # the next one costs a single request.
    return last, None
