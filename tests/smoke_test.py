"""Local smoke test: imports, config, legacy parser, platform detection.

Run from the project root: python -m tests.smoke_test
"""

import os
from pathlib import Path

os.environ.update(
    {
        "BOT_TOKEN": "42:TEST",
        "ADMIN_ID": "111111111",
        "DB_NAME": "x",
        "DB_USER": "x",
        "DB_PASSWORD": "p@ss:word/!",
        "DB_HOST": "localhost",
    }
)

from bot.config import load_settings
from bot.downloader import detect_platform, is_youtube_shorts, quality_format
from bot.handlers import create_root_router
from bot.legacy import parse_legacy_dump
from bot.urlcache import UrlCache

# --- config ---
settings = load_settings()
assert settings.admin_id == 111111111
assert settings.database_dsn == "postgresql://x:p%40ss%3Aword%2F%21@localhost:5432/x"
print("config OK:", settings.database_dsn)

# --- legacy parser ---
# info.txt is the export from the old bot and carries real people's ids, so it
# is not in the repository and cannot be. Where it exists the checks run against
# it; everywhere else - CI, a fresh clone - they run against a dump written here
# in the same shape, so the parser stays covered either way.
if Path("info.txt").exists():
    users, conversions = parse_legacy_dump(Path("info.txt"))
    print(f"parsed: {len(users)} users, {len(conversions)} conversions")
    assert len(users) == 499, len(users)
    assert len(conversions) == 7942, len(conversions)
    assert users[0].telegram_id == 6321925656 and users[0].username == "vitaIy04"
else:
    import tempfile as _tempfile

    _sample = chr(10).join([
        "USERS",
        "data: (1, 111, 'alice', '15/04/2025 14:57')",
        "data: (2, 222, None, '16/04/2025 09:01')",
        "CONVERTATIONS",
        "data: (1, 111, '15/04/2025 14:55', 'Done')",
        "data: (2, 222, '16/04/2025 09:05', 'Failed')",
    ])
    _dump = Path(_tempfile.mkdtemp(prefix='legacy-')) / 'info.txt'
    _dump.write_text(_sample, encoding='utf-8')
    users, conversions = parse_legacy_dump(_dump)
    print(f"parsed a written-here dump: {len(users)} users, {len(conversions)} conversions")
    assert len(users) == 2, len(users)
    assert len(conversions) == 2, len(conversions)
    assert users[0].telegram_id == 111 and users[0].username == "alice"

# A missing username has to survive as None rather than becoming the string.
assert any(u.username is None for u in users), "None usernames must survive parsing"
statuses = {c.status for c in conversions}
assert statuses == {"done", "failed"}, statuses
# Ordering and date parsing, stated so they hold for either dump.
assert conversions[0].id < conversions[-1].id
assert conversions[0].created_at.year == 2025
assert conversions[0].created_at.tzinfo is not None, 'timestamps must be aware'
print("legacy parser OK, statuses:", statuses)

# --- platform detection ---
cases = {
    "https://vm.tiktok.com/ZNd1KJEv9/": "tiktok",
    "https://www.tiktok.com/@user/video/1": "tiktok",
    "https://youtu.be/dQw4w9WgXcQ": "youtube",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "youtube",
    "https://www.youtube.com/shorts/abc": "youtube",
    "https://www.instagram.com/reel/abc/": "instagram",
    "https://www.pornhub.com/view_video.php?viewkey=abc": "pornhub",
    "https://rule34video.com/videos/123/x/": "rule34video",
    "https://rule34.xxx/index.php?page=post&s=view&id=1": "rule34",
    "https://www.the-joi-database.com/watch/abc123": "joidb",
    "https://vimeo.com/123": "other",
    "https://evil.com/?q=tiktok.com": "other",
}
for url, expected in cases.items():
    got = detect_platform(url)
    assert got == expected, f"{url}: {got} != {expected}"
assert is_youtube_shorts("https://www.youtube.com/shorts/abc")
assert not is_youtube_shorts("https://www.youtube.com/watch?v=abc")
assert "height=720" in quality_format(720)
print("platform detection OK")

# --- url cache ---
cache = UrlCache(max_size=3)
tokens = [cache.store(f"https://example.com/{i}") for i in range(5)]
assert cache.get(tokens[0]) is None, "oldest entries must be evicted"
assert cache.get(tokens[-1]) == "https://example.com/4"
assert all(len(f"audio:{t}".encode()) <= 64 for t in tokens), "callback_data over 64 bytes"
print("url cache OK")

# --- routers wire up ---
router = create_root_router(admin_id=111111111)
names = [r.name for r in router.sub_routers]
assert names == ["admin", "user", "download", "fallback"], names
print("routers OK:", names)

# --- internal-address guard ---
from bot.urlguard import BlockedAddressError, _is_public, ensure_public_url

for blocked in (
    "http://127.0.0.1:30080/health",
    "http://localhost/",
    "http://192.168.1.1/",
    "http://10.0.0.5/x",
    "http://172.17.0.1/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://[::1]/",
):
    try:
        ensure_public_url(blocked)
    except BlockedAddressError:
        pass
    else:
        raise AssertionError(f"must be refused: {blocked}")

# An IPv4 address tunnelled inside IPv6 must be judged as the IPv4 it really is.
assert not _is_public("::ffff:127.0.0.1"), "IPv4-mapped loopback must not pass"
assert not _is_public("::ffff:192.168.0.1"), "IPv4-mapped private must not pass"
assert _is_public("8.8.8.8") and _is_public("2001:4860:4860::8888")

# Search expressions carry no host and must not be refused.
ensure_public_url("ytsearch1:some track name")
print("internal-address guard OK")

# --- per-platform format sort ---
from bot.downloader import video_format_sort

# YouTube hides 4K behind vp9/av1 only, and phones show those as a frozen frame,
# so the codec has to outrank resolution there.
assert video_format_sort("youtube")[0] == "vcodec:h264"
assert video_format_sort("other")[0] == "vcodec:h264"
# TikTok was ranked by resolution first, to reach its h265 original instead of
# the smaller h264 transcode. Comparing what the bot delivered against a working
# downloader settled it the other way: the h265 file arrived as 1080p Matroska
# that Telegram played without sound, while h264 in mp4 simply plays. Sharper on
# paper loses to what the person can actually watch.
assert video_format_sort("tiktok")[0] == "vcodec:h264"
print("format sort OK")

# --- TikTok must take a ready-made file, never a merge ---
from bot.downloader import video_format

# Merging picked a video-only HEVC stream and laid the post's *music track* over
# it — a different clip, in Matroska, which Telegram played without sound.
tiktok_fmt = video_format("tiktok")
assert "vcodec!=none" in tiktok_fmt and "acodec!=none" in tiktok_fmt, tiktok_fmt
assert "+" not in tiktok_fmt, "a '+' means yt-dlp would merge two streams"
# Everywhere else merging is still how the best quality is assembled.
assert "+" in video_format("youtube")
# Among the muxed files h264 wins: Telegram's players stumble on HEVC.
assert video_format_sort("tiktok")[0] == "vcodec:h264"
print("tiktok format policy OK")

# --- a block is not reachability ---
from bot.proxy import _BLOCKED_STATUSES

# Cloudflare hands the server a 403 challenge while a proxy gets 200; counting
# that 403 as reachable routed the platform direct and broke every download.
assert 403 in _BLOCKED_STATUSES and 451 in _BLOCKED_STATUSES
assert 200 not in _BLOCKED_STATUSES and 404 not in _BLOCKED_STATUSES
print("block detection OK")

# --- one post, one cache key ---
from bot.db import hash_url
from bot.urlkey import canonical_key, is_short_link

# TikTok issues a new short link per share and appends a per-copy parameter to
# the address it expands to; every one of these is the same video.
same_video = [
    "https://www.tiktok.com/@tiktok/video/7106594312292453675",
    "https://www.tiktok.com/@tiktok/video/7106594312292453675/",
    "https://www.tiktok.com/@tiktok/video/7106594312292453675?is_from_webapp=1",
    "https://m.tiktok.com/@tiktok/video/7106594312292453675",
    "http://www.tiktok.com/@tiktok/video/7106594312292453675",
    # the username in a shared link is whoever reposted it — the id is what counts
    "https://www.tiktok.com/@someoneelse/video/7106594312292453675?_r=1&_t=ZS-9xYz",
]
assert len({hash_url(u) for u in same_video}) == 1, "one video must have one key"
assert canonical_key(same_video[0]) == "tiktok:7106594312292453675"

# Different posts must stay apart.
assert hash_url(same_video[0]) != hash_url(
    "https://www.tiktok.com/@tiktok/video/7106594312292453676")

for url, expected in [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=42s", "youtube:jNQXAC9IVRw"),
    ("https://www.youtube.com/shorts/abc123XYZ", "youtube:abc123XYZ"),
    ("https://www.instagram.com/reel/C1Ux1JYr7qF/", "instagram:C1Ux1JYr7qF"),
    ("https://x.com/nasa/status/1770458215432855942", "twitter:1770458215432855942"),
    ("https://rule34.xxx/index.php?page=post&s=view&id=7000000", "rule34:7000000"),
]:
    assert canonical_key(url) == expected, f"{url} -> {canonical_key(url)}"

# Unknown sites keep working: the address is normalised, not identified.
assert canonical_key("https://Example.COM/Watch/") == "example.com/Watch"

assert is_short_link("https://vt.tiktok.com/ZSVpTAqS7/")
assert not is_short_link("https://www.tiktok.com/@tiktok/video/7106594312292453675")
print("cache keys OK")


# --- VPN configs sent from the admin chat ---
import base64
import shutil
import sys
import tempfile

_vpn_root = Path(tempfile.mkdtemp(prefix="vpn-test-"))
os.environ["VPN_DIR"] = str(_vpn_root / "vpn")
os.environ["AWG_CONFIG_DIR"] = str(_vpn_root / "awg")
os.environ["XRAY_REBUILD_FILE"] = str(_vpn_root / "rebuild_xray")

import importlib

from bot import vpnstore

importlib.reload(vpnstore)

mixed = vpnstore.classify(
    "vless://u@a.com:443?type=ws" + chr(10)
    + "hy2://pw@b.com:443" + chr(10)
    + "https://sub.example/list" + chr(10)
    + "nonsense"
)
assert mixed.xray == ["vless://u@a.com:443?type=ws"], mixed.xray
assert mixed.singbox == ["hy2://pw@b.com:443"], mixed.singbox
assert mixed.subscriptions == ["https://sub.example/list"], mixed.subscriptions
assert mixed.unknown == 1

# A pasted subscription body is base64 of the link list, not links.
blob = base64.b64encode(
    ("vless://u@c.com:443" + chr(10) + "vless://u@d.com:443").encode()
).decode()
assert len(vpnstore.classify(blob).xray) == 2

awg_text = ("[Interface]" + chr(13) + chr(10) + "PrivateKey = k" + chr(13) + chr(10)
            + "Address = 10.8.1.6/32" + chr(13) + chr(10)
            + "[Peer]" + chr(13) + chr(10) + "Endpoint = 1.2.3.4:51820" + chr(13) + chr(10))
awg = vpnstore.classify(awg_text, "Fin-AWG.conf")
assert awg.awg and awg.awg_name == "Fin-AWG", awg.awg_name
# A filename is attacker-shaped input even from the admin: it must not escape.
assert vpnstore.safe_name("../../etc/passwd") == "etc-passwd"

vpnstore.apply_payload(mixed)
vpnstore.apply_payload(awg)
assert (_vpn_root / "rebuild_xray").exists(), "xray was not asked to rebuild"
assert (_vpn_root / "vpn" / "reload_singbox").exists()
assert (_vpn_root / "vpn" / "reload_awg").exists()
# The tunnel setup reads these fields with plain text tools; a carriage return
# turns an address into one ip(8) refuses.
assert chr(13) not in (_vpn_root / "awg" / "Fin-AWG.conf").read_text(encoding="utf-8")
shutil.rmtree(_vpn_root, ignore_errors=True)
print("vpn config intake OK")

# --- share links of every protocol the exits speak ---
sys.path.insert(0, str(Path("proxy").resolve()))
import build_config as _xray_build
import singbox_config as _singbox_build

vmess_link = "vmess://" + base64.b64encode(
    b'{"add":"1.2.3.4","port":443,"id":"u","net":"ws","tls":"tls","host":"h.com","path":"/p"}'
).decode()
for link, protocol in [
    ("vless://u@a.com:443?type=ws&security=tls", "vless"),
    (vmess_link, "vmess"),
    ("trojan://pass@1.2.3.4:443?sni=a.com", "trojan"),
    ("ss://" + base64.b64encode(b"aes-256-gcm:pw").decode() + "@1.2.3.4:8388", "shadowsocks"),
    ("ss://chacha20-ietf-poly1305:pw@1.2.3.4:8388", "shadowsocks"),
]:
    outbound = _xray_build.link_to_outbound(link, "t")
    assert outbound["protocol"] == protocol, (link, outbound["protocol"])

# Trojan is TLS by definition even when the link does not spell it out.
assert _xray_build.link_to_outbound(
    "trojan://p@a.com:443", "t")["streamSettings"]["security"] == "tls"

built = _singbox_build.build(["hy2://pw@a.com:443?sni=x.com", "tuic://uu:pp@b.com:8443"])
kinds = [o["type"] for o in built["outbounds"]]
assert kinds == ["urltest", "hysteria2", "tuic", "direct"], kinds
assert built["route"]["final"] == "auto"
print("proxy link parsers OK")


# --- upload deadline and retry safety ---
import asyncio as _asyncio

import aiohttp as _aiohttp
from aiogram.exceptions import TelegramNetworkError as _TelegramNetworkError

from bot.handlers.download import (
    _UPLOAD_KILL_SECONDS,
    _never_reached_telegram,
    _upload_timeout,
)

# A tiny clip must stop waiting in about a minute, not in eight.
assert _upload_timeout(750 * 1024) == 64, _upload_timeout(750 * 1024)
assert _upload_timeout(0) == 60
assert _upload_timeout(None) == 60
# A large file gets proportionally longer, but never past the point where the
# far side hangs up on its own - beyond that the deadline would never be ours.
assert _upload_timeout(50 * 1024 * 1024) == 360
assert _upload_timeout(2000 * 1024 * 1024) == _UPLOAD_KILL_SECONDS - 20
assert _upload_timeout(50 * 1024 * 1024 * 1024) < _UPLOAD_KILL_SECONDS


def _wrapped(cause: BaseException) -> _TelegramNetworkError:
    error = _TelegramNetworkError(method=None, message="x")
    error.__cause__ = cause
    return error


# Only a failure to connect proves the body never went out; those are safe to
# repeat. A disconnect while awaiting the response - the failure measured on
# 16 Sep - may already have delivered the video, so repeating it is what put the
# same clip in the chat three times.
_connection_key = _aiohttp.client_reqrep.ConnectionKey(
    "api.telegram.org", 443, False, True, None, None, None
)
assert _never_reached_telegram(
    _wrapped(_aiohttp.ClientConnectorError(_connection_key, OSError("refused")))
)
assert not _never_reached_telegram(_wrapped(_aiohttp.ServerDisconnectedError()))
assert not _never_reached_telegram(_wrapped(_asyncio.TimeoutError()))
assert not _never_reached_telegram(_wrapped(_aiohttp.ClientOSError("boom")))
print("upload deadline + retry safety OK")


# --- upload admission, admin reservation, per-user budget ---
from bot.handlers.download import (
    _RATE_TOKENS,
    _RATE_WINDOW_SECONDS,
    _STALE_AFTER_SECONDS,
    _UPLOAD_LANE_BYTES,
    _UPLOAD_LANE_LARGE,
    _UPLOAD_LANE_SMALL,
    _UPLOAD_TOTAL,
    _UploadCapacity,
    _rate_buckets,
    _rate_delay,
)

_SMALL = 1024
_BIG = _UPLOAD_LANE_BYTES + 1


async def _admission_checks() -> None:
    cap = _UploadCapacity()
    release = _asyncio.Event()
    holding = []

    async def hold(size, is_admin):
        async with cap.slot(size, is_admin):
            holding.append(size)
            await release.wait()

    # The small lane runs several at once. That extra room is the whole point:
    # one wedged upload used to hold the only place there was, for its whole
    # retry ladder, and everybody else waited behind it.
    room = min(_UPLOAD_LANE_SMALL, _UPLOAD_TOTAL)
    first = [_asyncio.create_task(hold(_SMALL, False)) for _ in range(room)]
    await _asyncio.sleep(0.05)
    assert len(holding) == room, holding

    # One more than the lane holds has to wait.
    third = _asyncio.create_task(hold(_SMALL, False))
    await _asyncio.sleep(0.05)
    assert len(holding) == room, "the lane must not admit past its width"

    # The admin is admitted with both user lanes full. This is the case that
    # deadlocks if the operator ever has to queue behind anybody: nothing would
    # release, because the thing holding the lanes is waiting on the operator.
    async def admin_upload():
        async with cap.slot(_BIG, True):
            return "admitted"

    assert await _asyncio.wait_for(admin_upload(), timeout=1) == "admitted"

    # Letting the first two go lets the waiter through.
    release.set()
    await _asyncio.wait_for(_asyncio.gather(*first, third), timeout=2)
    assert cap._small == 0 and cap._large == 0 and cap._admin == 0, (
        cap._small, cap._large, cap._admin,
    )

    # A large upload runs alone: two at once only halve each other and double the
    # time both spend exposed to a route that drops connections.
    release = _asyncio.Event()
    holding.clear()
    big = _asyncio.create_task(hold(_BIG, False))
    await _asyncio.sleep(0.05)
    assert len(holding) == 1
    second_big = _asyncio.create_task(hold(_BIG, False))
    await _asyncio.sleep(0.05)
    assert len(holding) == 1, "a second large upload must wait"
    release.set()
    await _asyncio.wait_for(_asyncio.gather(big, second_big), timeout=2)
    assert cap._large == 0


async def _reservation_check() -> None:
    """While the operator uploads, the users' small lane gives up a place."""
    cap = _UploadCapacity()
    release = _asyncio.Event()
    holding = []

    async def hold(size, is_admin):
        async with cap.slot(size, is_admin):
            holding.append(size)
            await release.wait()

    admin = _asyncio.create_task(hold(_SMALL, True))
    await _asyncio.sleep(0.05)
    # One place goes to the operator, so the users' room shrinks by one.
    room = max(1, min(_UPLOAD_LANE_SMALL, _UPLOAD_TOTAL) - 1)
    users = [_asyncio.create_task(hold(_SMALL, False)) for _ in range(room)]
    await _asyncio.sleep(0.05)
    assert len(holding) == room + 1, holding
    # The next one waits - but the lane is never closed outright, which is
    # what max(1, ...) guarantees.
    extra = _asyncio.create_task(hold(_SMALL, False))
    await _asyncio.sleep(0.05)
    assert len(holding) == room + 1, "the reservation must narrow the user lane"
    release.set()
    await _asyncio.wait_for(
        _asyncio.gather(admin, *users, extra), timeout=3
    )
    assert cap._small == 0 and cap._admin == 0


_asyncio.run(_admission_checks())
_asyncio.run(_reservation_check())

# Lane widths must stay consistent with the global cap, or the two numbers
# describe different systems and the one that binds is whichever is smaller.
assert _UPLOAD_LANE_LARGE <= _UPLOAD_TOTAL
assert _UPLOAD_TOTAL >= 1 and _UPLOAD_LANE_SMALL >= 1
assert _UPLOAD_LANE_BYTES >= 1024 * 1024

print("upload admission + admin reservation OK")

# The budget: a burst is fine, a flood is not, and it refills.
_rate_buckets.clear()
for _ in range(int(_RATE_TOKENS)):
    assert _rate_delay(4242) == 0.0
blocked = _rate_delay(4242)
assert blocked > 0, blocked
# What it reports is the time to earn one token back, never the whole window.
assert blocked <= _RATE_WINDOW_SECONDS / _RATE_TOKENS + 1, blocked
# Three a minute, so a place comes back within about twenty seconds - short
# enough that a person who simply types fast is not locked out, long enough
# that nobody can queue a fourth video while three are still ahead of it.
assert _RATE_WINDOW_SECONDS / _RATE_TOKENS <= 60, _RATE_WINDOW_SECONDS
_rate_buckets.clear()

# The gate is on the age of the MESSAGE when the bot picks it up, checked once
# before any work starts - not on how long a download runs. A file that needs
# forty minutes is unaffected, because the check is long behind it by then.
assert 60 <= _STALE_AFTER_SECONDS <= 3600, _STALE_AFTER_SECONDS
print("user budget + staleness OK")




# --- report timestamps are the operator's wall clock, not UTC ---
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

from bot.db import _REPORT_TZ, Database

_utc_noon = _dt(2026, 9, 17, 12, 0, tzinfo=_tz.utc)
assert f"{Database._local(_utc_noon):%d.%m.%y %H:%M}" == "17.09.26 15:00"
# Crossing midnight has to move the date too, which is exactly what adding
# three hours to an already-formatted string would have got wrong.
_late = _dt(2026, 9, 17, 23, 30, tzinfo=_tz.utc)
assert f"{Database._local(_late):%d.%m.%y %H:%M}" == "18.09.26 02:30"
assert Database._local(None) is None
assert _REPORT_TZ.utcoffset(None) == _td(hours=3)
print("report timezone OK")


# --- flood limit ---
# This one refuses messages before any handler sees them, so a mistake here is
# a bot that quietly ignores people. The admin must never be refused.
from bot.handlers.flood import FLOOD_MESSAGES, FLOOD_WINDOW_SECONDS, FloodMiddleware

_flood = FloodMiddleware(admin_id=999)
for _ in range(int(FLOOD_MESSAGES)):
    assert _flood._allow(4242), 'a normal burst must pass'
assert not _flood._allow(4242), 'past the burst it has to refuse'

# A different person is unaffected by someone else's flood.
assert _flood._allow(4243)

# Told once, then left alone: answering every message of a flood is a flood.
assert _flood._should_tell(4242)
assert not _flood._should_tell(4242)

# Seven a minute is above anyone typing and far below what the machine notices.
assert 3 <= FLOOD_MESSAGES <= 30, FLOOD_MESSAGES
assert FLOOD_WINDOW_SECONDS >= 10, FLOOD_WINDOW_SECONDS
# The flood ceiling must sit above the download budget, or the budget could
# never be reached and the two limits would be one.
assert FLOOD_MESSAGES > _RATE_TOKENS, (FLOOD_MESSAGES, _RATE_TOKENS)
print("flood limit OK")


# --- Instagram without an account ---
# The payload below is the shape measured from a real embed page on
# 18 Sep 2026: the post sits under gql_data.shortcode_media, a carousel hangs
# off edge_sidecar_to_children, and each item carries several resolutions.
# If Instagram moves any of that, this fails here rather than in a chat.
import json as _json

from bot.instagram import _parse as _ig_parse, shortcode as _ig_shortcode

assert _ig_shortcode('https://www.instagram.com/p/Cszjr-KsdT0/') == 'Cszjr-KsdT0'
assert _ig_shortcode('https://www.instagram.com/reel/DdXBVddvguJ/?x=1') == 'DdXBVddvguJ'
assert _ig_shortcode('https://www.instagram.com/someone/reel/AbC-123_x/') == 'AbC-123_x'
assert _ig_shortcode('https://www.tiktok.com/@a/video/1') is None


def _ig_page(payload: dict) -> str:
    # contextJSON is a JSON string inside the page's JSON, so it is encoded twice.
    return 'x = {"contextJSON":' + _json.dumps(_json.dumps(payload)) + '};'


_carousel = {'gql_data': {'shortcode_media': {
    '__typename': 'GraphSidecar',
    'owner': {'username': 'someone'},
    'edge_media_to_caption': {'edges': [{'node': {'text': '  a caption  '}}]},
    'edge_sidecar_to_children': {'edges': [
        {'node': {'is_video': False, 'display_url': 'https://cdn/small.jpg',
                  'display_resources': [
                      {'src': 'https://cdn/small.jpg', 'config_width': 640},
                      {'src': 'https://cdn/big.jpg', 'config_width': 1440}]}},
        {'node': {'is_video': True, 'video_url': 'https://cdn/clip.mp4',
                  'display_url': 'https://cdn/thumb.jpg'}},
    ]},
}}}
_post = _ig_parse(_ig_page(_carousel))
assert len(_post.items) == 2, _post.items
# The largest resolution, not the one sized for a feed.
assert _post.items[0].url == 'https://cdn/big.jpg' and not _post.items[0].is_video
# A video item is the video, never its thumbnail.
assert _post.items[1].url == 'https://cdn/clip.mp4' and _post.items[1].is_video
assert _post.caption == 'a caption'
assert _post.owner == 'someone'

_single = {'gql_data': {'shortcode_media': {
    '__typename': 'GraphVideo', 'is_video': True,
    'video_url': 'https://cdn/only.mp4', 'display_url': 'https://cdn/only.jpg',
}}}
_one = _ig_parse(_ig_page(_single))
assert len(_one.items) == 1 and _one.items[0].url == 'https://cdn/only.mp4'
assert _one.caption is None

# A private account or a removed post returns nothing, and nothing must be
# mistaken for a post with no media - the caller falls back on this.
assert not _ig_parse('<html>no payload here</html>')
assert not _ig_parse(_ig_page({'gql_data': {}}))

# A video whose URL the embed withholds must not come back as its cover
# frame. Measured on a real reel: GraphVideo, is_video true, video_duration
# and view count present, no video_url. Handing back the still would look
# like success and be wrong, so the post falls through to the session path.
_withheld = {'gql_data': {'shortcode_media': {
    '__typename': 'GraphVideo', 'is_video': True, 'video_duration': 12.3,
    'display_url': 'https://cdn/cover.jpg',
    'display_resources': [{'src': 'https://cdn/cover.jpg', 'config_width': 1080}],
}}}
assert not _ig_parse(_ig_page(_withheld)), 'a still must never stand in for a video'

# And one withheld item must not leave a carousel half delivered.
_mixed = {'gql_data': {'shortcode_media': {
    '__typename': 'GraphSidecar',
    'edge_sidecar_to_children': {'edges': [
        {'node': {'is_video': False, 'display_url': 'https://cdn/a.jpg'}},
        {'node': {'is_video': True, 'display_url': 'https://cdn/b.jpg'}},
    ]},
}}}
assert not _ig_parse(_ig_page(_mixed)), 'half a carousel is not the post'

# Whatever the reason an embed came back empty, it must not be reported as one
# the caller can act on. An earlier version read the page for a rendered image
# and called its absence proof of deletion, which stopped the session tools from
# running; measured against posts of known state it got a live post wrong, and
# that post downloads from those tools in three seconds. So the only two answers
# are served and not served.
import bot.instagram as _ig
import bot.downloader as _dl

assert not hasattr(_ig, 'GONE'), 'the embed must not have a verdict for deletion'
assert not hasattr(_dl, 'PostGoneError'), 'nothing may raise a deletion'
assert _ig._parse('<html>gated, no payload</html>').state == _ig.WITHHELD
assert _ig._parse('<html>EmbeddedMediaImage</html>').state == _ig.WITHHELD
# The one that used to be called gone: a share link behind a follow prompt.
assert _ig._parse('<html>no media, no payload, still a live post</html>').state == _ig.WITHHELD

print("instagram embed parsing OK")

# --- Instagram session watch -------------------------------------------------
# The bug this guards against is the one that already shipped once: a guess
# promoted to a verdict. Here the verdict is "the account is signed out", and
# what must never reach it is a request that simply failed.

import asyncio as _aio
import pathlib as _pathlib
import tempfile as _tmp

from bot import igsession as _igs

# What the probe makes of each reply, measured against a real session and two
# controls. The rule that matters: only the JSON in which Instagram itself
# reports nobody signed in counts as signed out.
assert _igs._read_answer(
    200, '{"config":{"viewer":{"username":"an_account"},"viewerId":"1"}}') == (
        _igs.LIVE, 'an_account')
assert _igs._read_answer(
    200, '{"config":{"csrf_token":"x","viewer":null,"viewerId":null}}') == (
        _igs.DEAD, None)

# The page this serves a half-remembered browser carries the account handle
# whether or not anybody is signed in. A version of this probe searched the body
# for that handle and called a login screen a live session, so the rendered page
# is now evidence of nothing.
assert _igs._read_answer(
    200, '<html>"username":"an_account" Create new account</html>')[0] == _igs.UNKNOWN

assert _igs._read_answer(200, 'not json')[0] == _igs.UNKNOWN
assert _igs._read_answer(200, '{"config":{}}')[0] == _igs.UNKNOWN
assert _igs._read_answer(200, '{"config":{"viewer":{"username":"  "}}}')[0] == _igs.UNKNOWN
assert _igs._read_answer(429, '{"config":{"viewer":null,"viewerId":"1"}}')[0] == _igs.UNKNOWN
assert _igs._read_answer(500, '')[0] == _igs.UNKNOWN


async def _note(bucket, text):
    bucket.append(text)


async def _fake_login(ok):
    return ok, 'fake'


async def _drive(jar, folder, probe_answers, login_ok):
    """One check() with the probe and the login faked, so what is measured is
    the decision-making rather than Instagram."""
    said = []
    watch = _igs.InstagramSession(
        jar, folder / 'req', folder / 'res',
        lambda text: _note(said, text),
    )
    answers = list(probe_answers)

    async def _fake_probe(_fn, _cookies, _proxy):
        return answers.pop(0) if answers else (_igs.UNKNOWN, None)

    watch._ask_for_login = lambda: _fake_login(login_ok)
    real = _aio.to_thread
    _aio.to_thread = _fake_probe
    try:
        first = await watch.check()
    finally:
        _aio.to_thread = real
    return watch, first, said


with _tmp.TemporaryDirectory() as _d:
    _dir = _pathlib.Path(_d)

    # A jar that cannot be read says nothing about Instagram, so: UNKNOWN. This
    # is the branch that keeps a local problem from being read as a remote one.
    assert _igs._probe_sync(_dir / 'missing.txt', None)[0] == _igs.UNKNOWN
    # A jar with no sessionid needs no network to be sure.
    _empty = _dir / 'empty.txt'
    _empty.write_text('# Netscape HTTP Cookie File' + '\n', encoding='utf-8')
    assert _igs._probe_sync(_empty, None)[0] == _igs.DEAD

    # A probe that could not be taken changes nothing and tells nobody. Without
    # this, every network blip would spend a login attempt.
    _w, _first, _said = _aio.run(_drive(_empty, _dir, [(_igs.UNKNOWN, None)], True))
    assert _first == _igs.UNKNOWN, _first
    assert not _said, _said

    # Signed out, the login works and the probe confirms it: recovered, and the
    # admin hears about it once.
    _w, _first, _said = _aio.run(
        _drive(_empty, _dir, [(_igs.DEAD, None), (_igs.LIVE, 'an_account')], True))
    assert _first == _igs.LIVE, _first
    assert len(_said) == 1 and 'Instagram' in _said[0], _said

    # A login that claims success while the session is still dead is not
    # believed - the probe is what settles it.
    _w, _first, _said = _aio.run(
        _drive(_empty, _dir, [(_igs.DEAD, None), (_igs.DEAD, None)], True))
    assert _first == _igs.DEAD, _first
    assert _w._failures == 1, _w._failures

    # Signed out and the login fails: the admin is told once, and the next
    # attempt is pushed into the future rather than retried on the spot.
    _w, _first, _said = _aio.run(_drive(_empty, _dir, [(_igs.DEAD, None)], False))
    assert _first == _igs.DEAD, _first
    assert len(_said) == 1, _said
    assert _w._next_attempt > 0, 'a failed login must back off'
    assert _w._failures == 1


    # A login that worked, followed by a probe that could not be taken, is
    # NOT a failed login - the two go out over different routes, so one
    # saying nothing carries no news about the other. Telling the operator
    # the login failed here would be this module's own rule broken against
    # them: an answer invented where there was none.
    _w, _first, _said = _aio.run(
        _drive(_empty, _dir, [(_igs.DEAD, None), (_igs.UNKNOWN, None)], True))
    assert not _said, 'an unconfirmed login must not be reported as failed'
    assert _w._next_attempt > 0, 'but it must still space the next attempt out'

    # If the warning could not be delivered, the watch has not warned
    # anybody - and must not believe it has. Otherwise the one message an
    # outage ever gets is the one that never arrived.
    async def _refuse(_text):
        raise RuntimeError('Telegram said no')

    _w2 = _igs.InstagramSession(_empty, _dir / 'req2', _dir / 'res2', _refuse)
    _w2._ask_for_login = lambda: _fake_login(False)

    async def _dead_probe(_fn, _cookies, _proxy):
        return (_igs.DEAD, None)

    _real_thread = _aio.to_thread
    _aio.to_thread = _dead_probe
    try:
        _aio.run(_w2.check())
    finally:
        _aio.to_thread = _real_thread
    assert _w2._told_admin_dead is False, (
        'a rejected send is not a warning delivered')


    # However often the session looks dead, the bot may not spend the day
    # signing in. A bug on the signed-in side once did exactly that, and
    # Instagram answered by restricting the account rather than the request.
    _w3 = _igs.InstagramSession(_empty, _dir / 'req3', _dir / 'res3', None)
    _asked = []

    async def _count_login():
        _asked.append(1)
        return False, 'nope'

    _w3._ask_for_login = _count_login

    async def _always_dead(_fn, _cookies, _proxy):
        return (_igs.DEAD, None)

    _real2 = _aio.to_thread
    _aio.to_thread = _always_dead
    try:
        for _ in range(12):
            _w3._next_attempt = 0.0   # pretend the backoff has elapsed
            _aio.run(_w3.check())
    finally:
        _aio.to_thread = _real2
    assert len(_asked) <= _igs._LOGINS_PER_DAY, (
        f'{len(_asked)} logins in a day, cap is {_igs._LOGINS_PER_DAY}')

print("instagram session watch OK")

# --- the browser resolver's side of the handshake ----------------------------
# The reading of the page happens in the other container; what is testable here
# is what this makes of the answer. The rule that matters: a refusal and a
# half-answer both become None, because a partial post that looks like a whole
# one is the failure this project keeps meeting.
import json as _rjson

from bot import igbrowser as _igb


def _resolved(payload, folder):
    req = folder / 'rq'
    res = folder / 'rs'
    res.write_text(_rjson.dumps(payload), encoding='utf-8')
    _igb.REQUEST_FILE, _igb.RESULT_FILE = req, res
    _igb.TIMEOUT = 6

    # The client deletes the result before asking, so put it back once the
    # request appears - standing in for the container that normally answers.
    async def _drive():
        task = _aio.ensure_future(_igb.resolve('https://www.instagram.com/p/X/'))
        for _ in range(20):
            await _aio.sleep(0.2)
            if req.exists() or not res.exists():
                res.write_text(_rjson.dumps(payload), encoding='utf-8')
                break
        return await task

    return _aio.run(_drive())


with _tmp.TemporaryDirectory() as _d2:
    _f = _pathlib.Path(_d2)
    _ok = _resolved({'ok': True, 'seconds': 3,
                     'items': [{'url': 'https://cdn/a.jpg', 'is_video': False},
                               {'url': 'https://cdn/b.mp4', 'is_video': True}],
                     'owner': 'someone', 'caption': 'hi'}, _f)
    assert _ok is not None and len(_ok.items) == 2, _ok
    assert _ok.items[0] == ('https://cdn/a.jpg', False)
    assert _ok.items[1] == ('https://cdn/b.mp4', True)
    assert _ok.owner == 'someone'

    # A refusal is not a post.
    assert _resolved({'ok': False, 'error': 'no media on the page'}, _f) is None
    # Nor is an empty one.
    assert _resolved({'ok': True, 'items': []}, _f) is None
    # An item with no URL is dropped rather than downloaded as nothing; if
    # that leaves none, the answer is None.
    assert _resolved({'ok': True, 'items': [{'is_video': True}]}, _f) is None

print('instagram browser resolver OK')

# --- the logged-out GraphQL read -------------------------------------------------
from bot import instagram as _igq

# Shortcode to media id, checked against a real post.
assert _igq._media_id('DaU8OdPxuwW') == '3933033250867833878'

# Largest candidate: by width when given; the first entry otherwise, because the
# first was measured to be the original size on every post checked.
assert _igq._largest([{'url': 's', 'width': 320}, {'url': 'L', 'width': 1080}]) == 'L'
assert _igq._largest([{'url': 'first'}, {'url': 'second'}]) == 'first'
assert _igq._largest([]) is None


def _gql(product):
    return _json.dumps({'data': {'xig_polaris_media': {
        'if_not_gated_logged_out': product}}})


_photo = _igq._parse_graphql(_gql({
    'code': 'X', 'media_type': 1, 'user': {'username': 'someone'},
    'caption': {'text': '  hi  '},
    'image_versions2': {'candidates': [{'url': 'https://cdn/big.jpg'},
                                       {'url': 'https://cdn/small.jpg'}]}}))
assert [(i.url, i.is_video) for i in _photo.items] == [('https://cdn/big.jpg', False)]
assert _photo.owner == 'someone' and _photo.caption == 'hi'

_reel = _igq._parse_graphql(_gql({
    'code': 'R', 'media_type': 2,
    'video_versions': [{'type': 101, 'url': 'https://cdn/v.mp4'}],
    'image_versions2': {'candidates': [{'url': 'https://cdn/cover.jpg'}]}}))
assert [(i.url, i.is_video) for i in _reel.items] == [('https://cdn/v.mp4', True)], (
    'a reel must come back as its video, never its cover')

_car = _igq._parse_graphql(_gql({
    'code': 'C', 'carousel_media': [
        {'image_versions2': {'candidates': [{'url': 'https://cdn/1.jpg'}]}},
        {'media_type': 2, 'video_versions': [{'url': 'https://cdn/2.mp4'}]}]}))
assert [i.is_video for i in _car.items] == [False, True]

# A video with no URL is not the post, and half a carousel is not either.
assert not _igq._parse_graphql(_gql({'code': 'W', 'media_type': 2,
    'image_versions2': {'candidates': [{'url': 'https://cdn/cover.jpg'}]}}))
# Gated, removed or refused: nothing, and no verdict about which.
assert not _igq._parse_graphql(_json.dumps({'data': {'xig_polaris_media': {
    'if_not_gated_logged_out': None, 'gating_ruling': {}}}}))
assert not _igq._parse_graphql(_json.dumps({'data': {'xig_polaris_media': {}}}))
assert not _igq._parse_graphql('not json')

# --- every Instagram road empty must end in 'not found', not a crash ---------
import bot.downloader as _dlz

_saved = (_dlz._instagram_items_sync, _dlz._download_album_sync,
          _dlz._instagram_browser_album)


def _empty_items(*_a, **_k):
    return [], None


def _gdl_fails(*_a, **_k):
    raise _dlz.DownloadFailedError('gallery-dl found nothing')


async def _browser_none(*_a, **_k):
    return None


async def _open_album():
    async with _dlz.download_album('https://www.instagram.com/p/AAAAAAAAAAA/', None):
        return 'opened'


_dlz._instagram_items_sync = _empty_items
_dlz._download_album_sync = _gdl_fails
_dlz._instagram_browser_album = _browser_none
try:
    try:
        _aio.run(_open_album())
        raise AssertionError('an empty post must not open as an album')
    except _dlz.DownloadFailedError:
        pass
finally:
    (_dlz._instagram_items_sync, _dlz._download_album_sync,
     _dlz._instagram_browser_album) = _saved

# While a CAPTCHA answer is fresh, the browser is not asked again: every page
# meets it, so each ask is eleven seconds and one more page load on a flagged
# account for an answer known in advance.
_igb._captcha_until = __import__('time').monotonic() + 60
assert _aio.run(_igb.resolve('https://www.instagram.com/p/X/')) is None
_igb._captcha_until = 0.0
print('instagram graphql reader OK')

# --- age-restricted posts go straight to the account roads -----------------------
# Only when Instagram says so in words - the one withheld answer allowed to
# change the route. Measured: gating_type 3, title "Age-restricted content".
_age_media = {'if_not_gated_logged_out': None, 'gating_ruling': {
    'gating_type': 3, 'title': 'Age-restricted content',
    'description': 'This content is age-restricted based on your age or account settings.'}}
assert _igq._gate_of(_age_media) == _igq.AGE_GATE
assert _igq._gate_of({'gating_ruling': {'title': 'Sensitive content'}}) is None
assert _igq._gate_of({}) is None
_gated = _igq._parse_graphql(_json.dumps({'data': {'xig_polaris_media': _age_media}}))
assert not _gated and _gated.gate == _igq.AGE_GATE
# An empty answer with no stated reason carries no gate - no inference.
assert _igq._parse_graphql(_json.dumps({'data': {'xig_polaris_media': {}}})).gate is None

# read_post skips the embed on a stated age gate (the embed is anonymous too).
_saved_gql = _igq.read_post_graphql
_embed_asked = []
_igq.read_post_graphql = lambda url, proxy=None: (_gated, None)
_saved_parse = _igq._parse
_igq._parse = lambda body: _embed_asked.append(1) or _igq.InstagramPost()
try:
    _p, _s = _igq.read_post('https://www.instagram.com/p/AAAAAAAAAAA/')
    assert _p.gate == _igq.AGE_GATE and _s is None
    assert not _embed_asked, 'the embed must not be asked about an age-gated post'
finally:
    _igq.read_post_graphql = _saved_gql
    _igq._parse = _saved_parse

# The album path: age gate -> gallery-dl -> browser, once, then an honest error.
_saved3 = (_dlz._instagram_items_sync, _dlz._download_album_sync,
           _dlz._instagram_browser_album)
_calls = {'gdl': 0, 'browser': 0}


def _age_items(*_a, **_k):
    raise _dlz.AgeRestrictedError(_dlz._AGE_RESTRICTED_MESSAGE)


def _gdl_count(*_a, **_k):
    _calls['gdl'] += 1
    raise _dlz.DownloadFailedError('redirect to home page')


async def _browser_count(*_a, **_k):
    _calls['browser'] += 1
    return None


_dlz._instagram_items_sync = _age_items
_dlz._download_album_sync = _gdl_count
_dlz._instagram_browser_album = _browser_count
try:
    try:
        _aio.run(_open_album())
        raise AssertionError('an age-gated post nobody could fetch must not open')
    except _dlz.AgeRestrictedError as _exc:
        assert 'возрастным' in str(_exc), str(_exc)
    assert _calls['browser'] == 1, _calls
    assert _calls['gdl'] == 1, ('the exit ladder cannot change an age gate', _calls)
finally:
    (_dlz._instagram_items_sync, _dlz._download_album_sync,
     _dlz._instagram_browser_album) = _saved3
print('instagram age gate OK')

# --- TikTok's login redirect ------------------------------------------------------
# Some short links now land on /login?redirect_url=<video>; the video is what the
# link means, and downloading the login page is what the bot used to do instead.
from bot.downloader import _unwrap_login_redirect as _unwrap

_login = ("https://www.tiktok.com/login?redirect_url=https%3A%2F%2Fwww.tiktok.com"
          "%2F%407d5.4%2Fvideo%2F7465397792442273031%3F_r%3D1%26_t%3DZS-9A0CeOlFsqg"
          "&lang=en&enter_method=mandatory")
assert _unwrap(_login).startswith(
    "https://www.tiktok.com/@7d5.4/video/7465397792442273031"), _unwrap(_login)
# Anything else passes through untouched.
assert _unwrap("https://www.tiktok.com/@a/video/1") == "https://www.tiktok.com/@a/video/1"
assert _unwrap("https://example.com/login?redirect_url=https://www.tiktok.com/@a/video/1") \
    == "https://example.com/login?redirect_url=https://www.tiktok.com/@a/video/1"
# A redirect that points off TikTok is not followed.
assert _unwrap("https://www.tiktok.com/login?redirect_url=https%3A%2F%2Fevil.example%2F") \
    .startswith("https://www.tiktok.com/login")
print('tiktok login redirect OK')




print("\nALL SMOKE TESTS PASSED")
