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
# TikTok ships the same clip as a smaller h264 transcode beside its h265
# original, so resolution has to come first or every download is downgraded.
assert video_format_sort("tiktok")[0] == "res"
assert "vcodec:h264" in video_format_sort("tiktok"), "h264 still breaks ties"
print("format sort OK")

# --- a block is not reachability ---
from bot.proxy import _BLOCKED_STATUSES

# Cloudflare hands the server a 403 challenge while a proxy gets 200; counting
# that 403 as reachable routed the platform direct and broke every download.
assert 403 in _BLOCKED_STATUSES and 451 in _BLOCKED_STATUSES
assert 200 not in _BLOCKED_STATUSES and 404 not in _BLOCKED_STATUSES
print("block detection OK")

# --- picking the better audio track ---
from bot.downloader import _AUDIO_GOOD_ENOUGH, _best_audio_only_format

# TikTok mixes a weak copy into the video and offers the real soundtrack on its
# own; only the audio-only entries may be considered for the swap.
formats = {"formats": [
    {"format_id": "video", "vcodec": "h264", "acodec": "aac", "abr": 64},
    {"format_id": "audio", "vcodec": "none", "acodec": "mp3", "abr": 128},
    {"format_id": "quieter", "vcodec": "none", "acodec": "mp3", "abr": 64},
]}
assert _best_audio_only_format(formats)["format_id"] == "audio"
assert _best_audio_only_format({"formats": []}) is None
# The skip threshold must not sit below what the separate track actually
# carries, or a file at 96 kbps would never be compared against a 128 kbps one.
assert _AUDIO_GOOD_ENOUGH >= 128000
print("audio upgrade rules OK")

# --- the audio swap must not change how long the video is ---
# A 24-second clip once came back as a minute of frozen frame: the standalone
# track is the whole song, and without -shortest the output runs as long as the
# longest input. Both directions are checked with files made on the spot.
import pathlib as _pl
import shutil as _shutil
import subprocess as _sp
import tempfile as _tf

if _shutil.which("ffmpeg") and _shutil.which("ffprobe"):
    from bot.downloader import _media_duration, _mux_better_audio

    def _make(path, seconds, kind):
        source = (["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=64x64:rate=10",
                   "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", "libx264",
                   "-c:a", "aac", "-shortest"] if kind == "video"
                  else ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"])
        _sp.run(["ffmpeg", "-y", "-v", "error", *source, str(path)], check=True, timeout=120)

    with _tf.TemporaryDirectory() as _d:
        _dir = _pl.Path(_d)
        _video, _long, _short = _dir / "v.mp4", _dir / "long.m4a", _dir / "short.m4a"
        _make(_video, 3, "video")
        _make(_long, 10, "audio")
        _make(_short, 1, "audio")

        _merged = _mux_better_audio(_video, _long)
        assert _merged != _video, "a longer track should still be attached"
        _got = _media_duration(_merged)
        assert _got is not None and abs(_got - 3) <= 1, f"clip stretched to {_got}s"

        # A track shorter than the clip would truncate the video — leave it alone.
        _make(_video, 3, "video")
        assert _mux_better_audio(_video, _short) == _video, "short track must be refused"
    print("audio swap keeps the clip length OK")
else:
    print("audio swap length check skipped (no ffmpeg)")

print("\nALL SMOKE TESTS PASSED")
