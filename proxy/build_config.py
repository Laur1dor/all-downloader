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
import sys
import urllib.parse
import urllib.request

SOCKS_PORT = int(os.getenv("XRAY_SOCKS_PORT", "2080"))
HTTP_PORT = int(os.getenv("XRAY_HTTP_PORT", "2081"))
PROBE_URL = os.getenv("XRAY_PROBE_URL", "https://www.google.com/generate_204")


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


def vless_to_outbound(link: str, tag: str) -> dict:
    body, _, _ = link[len("vless://"):].partition("#")
    userinfo, _, hostport = body.partition("@")
    hostpart, _, query = hostport.partition("?")
    host, _, port = hostpart.rpartition(":")
    params = dict(urllib.parse.parse_qsl(query))

    network = params.get("type", "tcp")
    security = params.get("security", "none")

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


def build_config(links: list[str]) -> dict:
    proxy_tags = []
    outbounds = []
    for index, link in enumerate(links):
        tag = f"vless-{index}"
        try:
            outbounds.append(vless_to_outbound(link, tag))
            proxy_tags.append(tag)
        except Exception as exc:
            print(f"WARN: cannot parse a config: {exc}", file=sys.stderr)

    if not proxy_tags:
        raise SystemExit("No usable VLESS configs found (check VLESS_SUBSCRIPTION/VLESS_CONFIGS).")

    outbounds += [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "block", "protocol": "blackhole"},
    ]

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
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
        ],
        "outbounds": outbounds,
        # Observatory pings each node; the balancer routes to the live, fastest one.
        "observatory": {
            "subjectSelector": ["vless-"],
            "probeUrl": PROBE_URL,
            "probeInterval": "60s",
        },
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [
                {"tag": "proxy", "selector": ["vless-"], "strategy": {"type": "leastPing"}}
            ],
            "rules": [{"type": "field", "inboundTag": ["socks", "http"], "balancerTag": "proxy"}],
        },
    }


def main() -> None:
    output = sys.argv[1] if len(sys.argv) > 1 else "/etc/xray/config.json"
    config = build_config(collect_links())
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    nodes = sum(1 for o in config["outbounds"] if o["tag"].startswith("vless-"))
    print(f"xray config written to {output}: {nodes} node(s)")


if __name__ == "__main__":
    main()
