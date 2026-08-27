"""Build an xray-core config from VLESS subscription(s) and direct config links.

Sources (env):
  VLESS_SUBSCRIPTION  comma-separated subscription URLs (base64 or plain list)
  VLESS_CONFIGS       comma/newline-separated vless:// links
  VLESS_CONFIGS_FILE  path to a file with one vless:// link per line

Output: an xray config.json with a SOCKS+HTTP inbound and a load-balanced set
of VLESS outbounds (observatory picks the lowest-latency live node, so a dead
node is dropped automatically).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import tomllib

ROUTING_FILE = os.getenv("ROUTING_FILE", "/app/data/routing.toml")

# Domains per platform, for pinning a dedicated node by domain-routing when a
# [services] entry in routing.toml is a vless:// link. Default is "<name>.com".
_SERVICE_DOMAINS = {
    "rule34": ["rule34.xxx"],
    "rule34video": ["rule34video.com"],
    "twitter": ["x.com", "twitter.com"],
    "joidb": ["the-joi-database.com"],
    "yandexmusic": ["music.yandex.ru", "music.yandex.com"],
    "tiktok": ["tiktok.com"],
    "youtube": ["youtube.com", "youtu.be"],
    "instagram": ["instagram.com"],
}


def _pinned_services() -> dict[str, str]:
    """Platforms whose routing.toml policy is a vless:// link → dedicated node."""
    try:
        with open(ROUTING_FILE, "rb") as fh:
            services = tomllib.load(fh).get("services", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {k: v for k, v in services.items() if isinstance(v, str) and v.startswith("vless://")}

SOCKS_PORT = int(os.getenv("XRAY_SOCKS_PORT", "2080"))
HTTP_PORT = int(os.getenv("XRAY_HTTP_PORT", "2081"))
PROBE_URL = os.getenv("XRAY_PROBE_URL", "https://www.google.com/generate_204")

# Free public pool (e.g. AvenCores/goida-vpn-configs). Exposed on its own SOCKS
# port with a random-pick balancer, so the main node stays clean and a separate
# pool of throwaway nodes is available for IP-banned sites / main-node fallback.
GOIDA_SOCKS_PORT = int(os.getenv("GOIDA_SOCKS_PORT", "2079"))
GOIDA_POOL_SIZE = int(os.getenv("GOIDA_POOL_SIZE", "50"))
# Disk cache of the last good goida link set. The free aggregators redirect to
# raw.githubusercontent.com, which is DPI-throttled from the target region and
# intermittently returns nothing — a fresh fetch that yields (almost) no nodes
# would otherwise empty the whole pool and break every goida-routed site until
# the next lucky refresh. We persist the last healthy set and reuse it whenever a
# fetch comes back short, so the pool never collapses to zero.
GOIDA_CACHE = os.getenv("GOIDA_CACHE", "/app/data/goida_cache.txt")
GOIDA_MIN = int(os.getenv("GOIDA_MIN", "5"))

# Curated "bypass" pool: a few premium VLESS nodes (in BYPASS_CONFIGS) on their
# own SOCKS port, leastPing with health failover — for sites that block by IP
# reputation (e.g. DDoS-Guard) where the free/main exits get blocked.
BYPASS_SOCKS_PORT = int(os.getenv("BYPASS_SOCKS_PORT", "2078"))


def collect_bypass_links() -> list[str]:
    inline = os.getenv("BYPASS_CONFIGS", "").replace(",", "\n")
    return [ln.strip() for ln in inline.splitlines() if ln.strip().startswith("vless://")]


def _b64_maybe(text: str) -> str:
    stripped = "".join(text.split())
    try:
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode("utf-8")
        if "://" in decoded:
            return decoded
    except Exception:
        pass
    return text


# GitHub answers 403 to some client UAs; a browser UA is served normally. Kept as
# a list so a source that dislikes one identity is still reachable with another.
_FETCH_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "v2rayN/6.0",
)


def _fetch(url: str) -> str:
    last_exc: Exception | None = None
    for ua in _FETCH_UAS:
        request = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _mirror_urls(url: str) -> list[str]:
    """Alternate URLs for a GitHub raw link. github.com/raw and
    raw.githubusercontent.com both 302/route to Fastly, which is DPI-throttled
    from the target region; jsdelivr's CDN is usually reachable, so try it too.
    Returns the original plus any mirrors, de-duplicated, original first."""
    urls = [url]
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/raw/refs/heads/([^/]+)/(.+)", url
    ) or re.match(
        r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/refs/heads/([^/]+)/(.+)",
        url,
    )
    if m:
        owner, repo, branch, path = m.groups()
        urls.append(f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{branch}/{path}")
    return list(dict.fromkeys(urls))


def _fetch_first(url: str) -> str:
    """Fetch a subscription trying its mirrors in turn; first non-empty wins."""
    last_exc: Exception | None = None
    for candidate in _mirror_urls(url):
        try:
            text = _b64_maybe(_fetch(candidate))
            if text.strip():
                return text
        except Exception as exc:  # try the next mirror
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return ""


def _read_goida_cache() -> list[str]:
    try:
        with open(GOIDA_CACHE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    except OSError:
        return []


def _write_goida_cache(links: list[str]) -> None:
    try:
        os.makedirs(os.path.dirname(GOIDA_CACHE) or ".", exist_ok=True)
        with open(GOIDA_CACHE, "w", encoding="utf-8") as fh:
            fh.write("\n".join(links))
    except OSError as exc:
        print(f"WARN: could not write goida cache {GOIDA_CACHE!r}: {exc}", file=sys.stderr)


def collect_links() -> list[str]:
    links: list[str] = []
    for sub in filter(None, (s.strip() for s in os.getenv("VLESS_SUBSCRIPTION", "").split(","))):
        try:
            links += [ln.strip() for ln in _b64_maybe(_fetch(sub)).splitlines() if "://" in ln]
        except Exception as exc:
            print(f"WARN: subscription {sub!r} failed: {exc}", file=sys.stderr)
    inline = os.getenv("VLESS_CONFIGS", "").replace(",", "\n")
    links += [ln.strip() for ln in inline.splitlines() if ln.strip().startswith("vless://")]
    path = os.getenv("VLESS_CONFIGS_FILE", "")
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            links += [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    # de-duplicate, keep order
    seen: list[str] = []
    for link in links:
        if link not in seen:
            seen.append(link)
    return seen


# Russian exits are useless here (the bot serves a region that blocks the very
# sites it downloads from), so they are skipped by their share-link label.
_RU_MARKERS = ("\U0001f1f7\U0001f1fa", "russia", "россия", "russian")


def _is_russian(link: str) -> bool:
    label = link.split("#", 1)[1] if "#" in link else ""
    label = urllib.parse.unquote(label)
    return any(m in label or m in label.lower() for m in _RU_MARKERS)


GOIDA_LIVE_FILE = os.getenv("GOIDA_LIVE_FILE", "/app/data/goida_live.txt")
# Older than this the verified set is not trusted: free nodes die within hours.
GOIDA_LIVE_MAX_AGE = int(os.getenv("GOIDA_LIVE_MAX_AGE", "10800"))


def _read_probed_live() -> list[str]:
    """Nodes the prober confirmed working, if the file is recent enough."""
    try:
        if time.time() - os.path.getmtime(GOIDA_LIVE_FILE) > GOIDA_LIVE_MAX_AGE:
            return []
        with open(GOIDA_LIVE_FILE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    except OSError:
        return []


def collect_goida_links() -> list[str]:
    """VLESS links from the free public subscriptions, taken in order, capped.

    The aggregators mix many protocols; we keep only VLESS (the majority) so no
    extra parsers are needed, drop Russian exits, and cap the count so the
    observatory stays light.
    """
    # The prober (probe_goida.py) keeps a set of nodes that were verified to
    # actually carry traffic. Roughly 2% of the free links do, so a pool filled
    # straight from the subscriptions is almost all dead weight; prefer the
    # verified set whenever it is fresh.
    live = _read_probed_live()
    if live:
        print(f"goida: {len(live)} verified node(s) from the prober", file=sys.stderr)
        return live

    urls = [s.strip() for s in os.getenv("GOIDA_SUBSCRIPTIONS", "").split(",") if s.strip()]
    per_source: list[list[str]] = []
    for url in urls:
        try:
            text = _fetch_first(url)
        except Exception as exc:
            print(f"WARN: goida subscription {url!r} failed: {exc}", file=sys.stderr)
            continue
        per_source.append([
            ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("vless://") and not _is_russian(ln.strip())
        ])
    # Round-robin across sources so every subscription contributes (a plain
    # in-order fill would take the whole cap from the first file); de-duplicate,
    # capped so the observatory stays light.
    seen: list[str] = []
    seen_set: set[str] = set()
    index = 0
    while per_source and len(seen) < GOIDA_POOL_SIZE:
        progressed = False
        for links in per_source:
            if index < len(links):
                progressed = True
                link = links[index]
                if link not in seen_set:
                    seen_set.add(link)
                    seen.append(link)
                    if len(seen) >= GOIDA_POOL_SIZE:
                        break
        if not progressed:
            break
        index += 1
    # A short/empty fetch (DPI throttled the CDN) must not empty the pool: reuse
    # the last good set from disk. Only overwrite the cache when we got a healthy
    # batch, so a bad fetch can never poison it.
    if len(seen) >= GOIDA_MIN:
        _write_goida_cache(seen)
        return seen
    cached = _read_goida_cache()
    if cached:
        print(f"WARN: goida fetch returned {len(seen)} node(s); reusing "
              f"{len(cached)} cached node(s)", file=sys.stderr)
        return cached
    return seen


def _links_to_outbounds(links: list[str], prefix: str) -> tuple[list[dict], list[str]]:
    outbounds, tags = [], []
    for index, link in enumerate(links):
        tag = f"{prefix}{index}"
        try:
            outbounds.append(vless_to_outbound(link, tag))
            tags.append(tag)
        except Exception as exc:
            print(f"WARN: cannot parse a config: {exc}", file=sys.stderr)
    return outbounds, tags


def vless_to_outbound(link: str, tag: str) -> dict:
    body, _, _ = link[len("vless://"):].partition("#")
    userinfo, _, hostport = body.partition("@")
    hostpart, _, query = hostport.partition("?")
    # Drop any /path so it can't taint the port (free links carry host:port/path).
    hostpart = hostpart.split("/", 1)[0]
    host, _, port = hostpart.rpartition(":")
    port = "".join(c for c in port if c.isdigit())  # strip stray non-digits
    params = dict(urllib.parse.parse_qsl(query))

    network = params.get("type", "tcp")
    security = params.get("security", "none")
    # Free configs sometimes carry junk (e.g. security=false) that makes xray
    # reject the whole config — clamp to a value xray accepts.
    if security not in ("reality", "tls", "none"):
        security = "none"

    stream: dict = {"network": network, "security": security}

    if security == "reality":
        stream["realitySettings"] = {
            "publicKey": params.get("pbk", ""),
            "shortId": params.get("sid", ""),
            "serverName": params.get("sni", ""),
            "fingerprint": params.get("fp", "chrome"),
            "spiderX": params.get("spx", ""),
        }
    elif security == "tls":
        stream["tlsSettings"] = {
            "serverName": params.get("sni", host),
            "fingerprint": params.get("fp", "chrome"),
            "allowInsecure": params.get("allowInsecure") == "1",
            **({"alpn": params["alpn"].split(",")} if params.get("alpn") else {}),
        }

    if network == "xhttp":
        xhttp: dict = {
            "host": params.get("host", ""),
            "path": params.get("path", "/"),
            "mode": params.get("mode", "auto"),
        }
        # The share link bundles advanced fields (headers, sc*, padding) inside an
        # `extra` JSON; xray expects them as top-level xhttpSettings keys.
        if params.get("extra"):
            try:
                xhttp.update(json.loads(urllib.parse.unquote(params["extra"])))
            except ValueError:
                pass
        stream["xhttpSettings"] = xhttp
    elif network == "ws":
        stream["wsSettings"] = {
            "path": params.get("path", "/"),
            "headers": {"Host": params["host"]} if params.get("host") else {},
        }
    elif network == "grpc":
        stream["grpcSettings"] = {
            "serviceName": params.get("serviceName", params.get("path", "").lstrip("/")),
            "multiMode": params.get("mode") == "multi",
        }
    elif network in ("http", "h2", "httpupgrade"):
        stream[("httpupgradeSettings" if network == "httpupgrade" else "httpSettings")] = {
            "path": params.get("path", "/"),
            "host": [params["host"]] if params.get("host") else [],
        }

    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": host,
                    "port": int(port or 443),
                    "users": [
                        {
                            "id": userinfo,
                            # VLESS mandates encryption "none"; free links often
                            # omit it or carry junk, which makes xray reject the
                            # whole config, so it is forced here.
                            "encryption": "none",
                            "flow": params.get("flow", ""),
                        }
                    ],
                }
            ]
        },
        "streamSettings": stream,
    }


def build_config(
    links: list[str],
    goida_links: list[str] | None = None,
    bypass_links: list[str] | None = None,
) -> dict:
    outbounds, proxy_tags = _links_to_outbounds(links, "vless-")

    goida_outbounds, goida_tags = _links_to_outbounds(goida_links or [], "gvless-")
    outbounds += goida_outbounds
    bypass_outbounds, bypass_tags = _links_to_outbounds(bypass_links or [], "byp-")
    outbounds += bypass_outbounds

    outbounds += [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ]

    inbounds = [
        {
            "tag": "socks",
            "listen": "0.0.0.0",
            "port": SOCKS_PORT,
            "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
        },
        {
            "tag": "http",
            "listen": "0.0.0.0",
            "port": HTTP_PORT,
            "protocol": "http",
        },
    ]
    # Main pool: lowest-ping live node. Routed from the socks/http inbounds.
    # The personal subscription is optional: with no main nodes configured the
    # main socks/http inbounds stay up and are served by whichever pool exists,
    # so PROXY_URL keeps working instead of xray refusing to build a config.
    balancers = []
    rules = []
    if proxy_tags:
        balancers.append(
            {"tag": "proxy", "selector": ["vless-"], "strategy": {"type": "leastPing"}}
        )
        rules.append({"type": "field", "inboundTag": ["socks", "http"], "balancerTag": "proxy"})
    elif goida_tags or bypass_tags:
        # Borrow the pool's own strategy: gvless- is health-checked by the burst
        # observatory (leastLoad), byp- by the plain observatory (leastPing).
        if goida_tags:
            fallback: list[str] = ["gvless-"]
            strategy: dict = {"type": "leastLoad", "settings": {"expected": 8, "maxRTT": "4s"}}
        else:
            fallback, strategy = ["byp-"], {"type": "leastPing"}
        balancers.append({"tag": "proxy", "selector": fallback, "strategy": strategy})
        rules.append({"type": "field", "inboundTag": ["socks", "http"], "balancerTag": "proxy"})
        print("WARN: no main VLESS nodes — main socks/http served by the "
              f"{'goida' if goida_tags else 'bypass'} pool", file=sys.stderr)
    else:
        rules.append({"type": "field", "inboundTag": ["socks", "http"], "outboundTag": "direct"})
        print("WARN: no VLESS nodes at all — main socks/http goes out direct",
              file=sys.stderr)

    # Per-service pinned nodes (routing.toml entries that are vless:// links):
    # a dedicated outbound + a domain rule that wins over the balancer.
    for service, link in _pinned_services().items():
        tag = f"svc-{service}"
        try:
            outbounds.append(vless_to_outbound(link, tag))
        except Exception as exc:
            print(f"WARN: pinned node for {service} unparsable: {exc}", file=sys.stderr)
            continue
        domains = _SERVICE_DOMAINS.get(service, [f"{service}.com"])
        rules.insert(0, {
            "type": "field",
            "domain": [f"domain:{d}" for d in domains],
            "outboundTag": tag,
        })

    # Free goida pool on its own inbound. leastLoad over a burst-health-checked
    # set rotates among the *live* nodes (random over all would keep hitting dead
    # ones); dead nodes drop out continuously, nothing is cached.
    burst = None
    if goida_tags:
        inbounds.append({
            "tag": "goida-socks",
            "listen": "0.0.0.0",
            "port": GOIDA_SOCKS_PORT,
            "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
        })
        balancers.append({
            "tag": "goida",
            "selector": ["gvless-"],
            "strategy": {"type": "leastLoad", "settings": {"expected": 8, "maxRTT": "4s"}},
        })
        rules.insert(
            0, {"type": "field", "inboundTag": ["goida-socks"], "balancerTag": "goida"}
        )
        burst = {
            "subjectSelector": ["gvless-"],
            "pingConfig": {
                "destination": PROBE_URL,
                "interval": "90s",
                "sampling": 2,
                "timeout": "8s",
            },
        }

    # Curated bypass pool on its own inbound: leastPing with health failover.
    observatory_subjects = ["vless-"]
    if bypass_tags:
        inbounds.append({
            "tag": "bypass-socks",
            "listen": "0.0.0.0",
            "port": BYPASS_SOCKS_PORT,
            "protocol": "socks",
            "settings": {"udp": True, "auth": "noauth"},
        })
        balancers.append(
            {"tag": "bypass", "selector": ["byp-"], "strategy": {"type": "leastPing"}}
        )
        rules.insert(
            0, {"type": "field", "inboundTag": ["bypass-socks"], "balancerTag": "bypass"}
        )
        observatory_subjects.append("byp-")

    config = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        # Observatory continuously pings the main + bypass pools; dead nodes drop
        # from their leastPing balancers automatically (no stale pick survives).
        "observatory": {
            "subjectSelector": observatory_subjects,
            "probeUrl": PROBE_URL,
            "probeInterval": "60s",
        },
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": balancers,
            "rules": rules,
        },
    }
    if burst is not None:
        config["burstObservatory"] = burst
    return config


def _xray_test(path: str) -> tuple[bool, str | None]:
    """Validate the config with xray. Returns (ok, offending_tag).

    When xray rejects the config it names the outbound at fault
    ("...with tag gvless-9..."); that tag lets the caller drop just the bad node
    instead of the whole pool. ok=True (with no tag) also when xray can't be run.
    """
    try:
        r = subprocess.run(["xray", "run", "-test", "-c", path],
                           capture_output=True, timeout=40)
    except (OSError, subprocess.SubprocessError):
        return True, None  # can't test (e.g. binary missing) → don't block startup
    if r.returncode == 0:
        return True, None
    err = r.stderr.decode("utf-8", "ignore") + r.stdout.decode("utf-8", "ignore")
    match = re.search(r"tag\s+(\S+)", err)
    # The tag is quoted/punctuated in some messages ("...with tag gvless-9:"),
    # and a tag that still carries them matches no outbound.
    tag = match.group(1).strip("\"'`,.:;()[]") if match else None
    return False, (tag or None)


def _write(path: str, config: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)


_LINK_ENDPOINT_RE = re.compile(r"vless://[^@]+@([^:/?#]+):(\d+)")


def _endpoint_of_outbound(config: dict, tag: str) -> str | None:
    """host:port an outbound dials, so a rejected tag maps back to its link."""
    for outbound in config["outbounds"]:
        if outbound.get("tag") == tag:
            servers = outbound.get("settings", {}).get("vnext") or []
            if servers:
                return f"{servers[0].get('address')}:{servers[0].get('port')}"
    return None


def _without_endpoint(links: list[str], endpoint: str | None) -> list[str]:
    """The link list minus the node dialling `endpoint`."""
    if not endpoint:
        return links
    kept = []
    for link in links:
        m = _LINK_ENDPOINT_RE.match(link)
        if m and f"{m.group(1)}:{m.group(2)}" == endpoint:
            continue
        kept.append(link)
    return kept


def _tester_works() -> bool:
    """Whether `xray run -test` can be trusted right now.

    A trivial config that xray must accept. If even that is rejected, the
    failure is the environment (the box is starved while the prober dials
    nodes), not the config under test — and a healthy pool must not be thrown
    away over it.
    """
    path = "/tmp/xray-test-canary.json"
    _write(path, {"log": {"loglevel": "none"},
                  "inbounds": [],
                  "outbounds": [{"tag": "direct", "protocol": "freedom"}]})
    ok, _ = _xray_test(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    return ok


def main() -> None:
    output = sys.argv[1] if len(sys.argv) > 1 else "/etc/xray/config.json"
    links, bypass = collect_links(), collect_bypass_links()
    goida = collect_goida_links()
    config = build_config(links, goida, bypass)
    _write(output, config)
    # A single malformed free node makes xray reject the whole config and never
    # start (which also takes down the Telegram tunnel through it). Drop just the
    # node xray names and re-test, so the pool survives bad nodes; only if that
    # can't be resolved fall back to rebuilding without the goida pool entirely.
    for _ in range(40):
        ok, tag = _xray_test(output)
        if ok:
            break
        dropped = [o for o in config["outbounds"] if o["tag"] != tag]
        if tag and len(dropped) < len(config["outbounds"]):
            print(f"WARN: dropping node {tag} rejected by xray", file=sys.stderr)
            if tag.startswith("gvless-"):
                # Rebuild from the shortened link list rather than deleting the
                # outbound in place: the balancers and the observatory selector
                # are derived from it and would otherwise point at a gap.
                shorter = _without_endpoint(goida, _endpoint_of_outbound(config, tag))
                goida = shorter if len(shorter) < len(goida) else goida[:-1]
                config = build_config(links, goida, bypass)
            else:
                config["outbounds"] = dropped
            _write(output, config)
            continue
        # xray refused without naming a usable outbound. Before blaming the
        # nodes, check the tester itself: under load it has rejected configs
        # that validate fine moments later, and bisecting on that signal walked
        # a healthy 25-node pool down to nothing.
        if not _tester_works():
            print("WARN: xray -test is unreliable right now — keeping the config as built",
                  file=sys.stderr)
            break
        # Halving the free pool isolates the offender in a few rounds; dropping
        # the pool outright cost every free node because of one bad config.
        goida = goida[: len(goida) // 2]
        if not goida:
            print("WARN: config still rejected — rebuilding without goida pool",
                  file=sys.stderr)
            config = build_config(links, [], bypass)
            _write(output, config)
            break
        print(f"WARN: config rejected without a usable tag — retrying with "
              f"{len(goida)} goida node(s)", file=sys.stderr)
        config = build_config(links, goida, bypass)
        _write(output, config)
    n = lambda p: sum(1 for o in config["outbounds"] if o["tag"].startswith(p))  # noqa: E731
    print(f"xray config: {output} — {n('vless-')} main, {n('gvless-')} goida, "
          f"{n('byp-')} bypass node(s)")


if __name__ == "__main__":
    main()
