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
import subprocess
import sys
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


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "v2rayN/6.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "ignore")


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


def collect_goida_links() -> list[str]:
    """VLESS links from the free public subscriptions, taken in order, capped.

    The aggregators mix many protocols; we keep only VLESS (the majority) so no
    extra parsers are needed, drop Russian exits, and cap the count so the
    observatory stays light.
    """
    urls = [s.strip() for s in os.getenv("GOIDA_SUBSCRIPTIONS", "").split(",") if s.strip()]
    seen: list[str] = []
    for url in urls:
        try:
            text = _b64_maybe(_fetch(url))
        except Exception as exc:
            print(f"WARN: goida subscription {url!r} failed: {exc}", file=sys.stderr)
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("vless://") and line not in seen and not _is_russian(line):
                seen.append(line)
                if len(seen) >= GOIDA_POOL_SIZE:
                    return seen
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
    host, _, port = hostpart.rpartition(":")
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
                            "encryption": params.get("encryption", "none"),
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
    if not proxy_tags:
        raise SystemExit("No usable VLESS configs found (check VLESS_SUBSCRIPTION/VLESS_CONFIGS).")

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
    balancers = [{"tag": "proxy", "selector": ["vless-"], "strategy": {"type": "leastPing"}}]
    rules = [{"type": "field", "inboundTag": ["socks", "http"], "balancerTag": "proxy"}]

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


def _xray_accepts(path: str) -> bool:
    """True if xray can load the config. A single malformed free node would
    otherwise make xray reject the whole config and never start."""
    try:
        r = subprocess.run(["xray", "run", "-test", "-c", path],
                           capture_output=True, timeout=40)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # can't test (e.g. binary missing) → don't block startup


def _write(path: str, config: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)


def main() -> None:
    output = sys.argv[1] if len(sys.argv) > 1 else "/etc/xray/config.json"
    links, bypass = collect_links(), collect_bypass_links()
    goida = collect_goida_links()
    config = build_config(links, goida, bypass)
    _write(output, config)
    # The main + bypass pools are curated/valid; only the free goida pool can
    # carry a node xray rejects. If the full config fails to load, drop goida so
    # xray (and the whole bot, which tunnels through it) always comes up.
    if goida and not _xray_accepts(output):
        print("WARN: config rejected by xray — rebuilding without the goida pool",
              file=sys.stderr)
        config = build_config(links, [], bypass)
        _write(output, config)
    n = lambda p: sum(1 for o in config["outbounds"] if o["tag"].startswith(p))  # noqa: E731
    print(f"xray config: {output} — {n('vless-')} main, {n('gvless-')} goida, "
          f"{n('byp-')} bypass node(s)")


if __name__ == "__main__":
    main()
