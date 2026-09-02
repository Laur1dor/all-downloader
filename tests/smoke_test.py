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

# --- legacy parser on the real dump ---
users, conversions = parse_legacy_dump(Path("info.txt"))
print(f"parsed: {len(users)} users, {len(conversions)} conversions")
assert len(users) == 499, len(users)
assert len(conversions) == 7942, len(conversions)
assert users[0].telegram_id == 6321925656 and users[0].username == "vitaIy04"
assert any(u.username is None for u in users), "None usernames must survive parsing"
statuses = {c.status for c in conversions}
assert statuses == {"done", "failed"}, statuses
assert conversions[-1].id == 8094
assert conversions[0].created_at.year == 2025
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



print("\nALL SMOKE TESTS PASSED")
