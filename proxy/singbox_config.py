"""Turn hysteria2/tuic share links into a sing-box config with a SOCKS inbound.

xray-core does not speak either protocol, and both are what providers hand out
when the transport has to survive a link that drops TCP connections — which is
the case this proxy exists for. So the configs the admin sends to /vpn are split
by scheme and the ones xray cannot run end up here.

Links come from ``data/vpn/singbox.txt``, written by the bot; the entrypoint
rebuilds and restarts when that file changes.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse

LINKS_FILE = os.getenv("SINGBOX_LINKS", "/app/data/vpn/singbox.txt")
SOCKS_PORT = int(os.getenv("SINGBOX_SOCKS_PORT", "2081"))
TEST_URL = "https://www.gstatic.com/generate_204"


def _split(link: str) -> tuple[str, str, str, int, dict]:
    """(scheme, userinfo, host, port, query params) for one share link."""
    scheme, _, rest = link.partition("://")
    body = rest.partition("#")[0]
    userinfo, _, hostport = body.rpartition("@")
    hostpart, _, query = hostport.partition("?")
    hostpart = hostpart.split("/", 1)[0]
    host, _, port = hostpart.rpartition(":")
    digits = "".join(c for c in port if c.isdigit())
    return (
        scheme.lower(),
        urllib.parse.unquote(userinfo),
        host,
        int(digits or 443),
        dict(urllib.parse.parse_qsl(query)),
    )


def _tls(params: dict, host: str) -> dict:
    tls = {
        "enabled": True,
        "server_name": params.get("sni") or params.get("peer") or host,
        "insecure": params.get("insecure") in ("1", "true"),
    }
    if params.get("alpn"):
        tls["alpn"] = params["alpn"].split(",")
    return tls


def link_to_outbound(link: str, tag: str) -> dict:
    scheme, userinfo, host, port, params = _split(link)
    if scheme in ("hysteria2", "hy2"):
        outbound = {
            "type": "hysteria2",
            "tag": tag,
            "server": host,
            "server_port": port,
            "password": userinfo,
            "tls": _tls(params, host),
        }
        if params.get("obfs") == "salamander" and params.get("obfs-password"):
            outbound["obfs"] = {
                "type": "salamander",
                "password": params["obfs-password"],
            }
        return outbound
    if scheme == "tuic":
        uuid, _, password = userinfo.partition(":")
        return {
            "type": "tuic",
            "tag": tag,
            "server": host,
            "server_port": port,
            "uuid": uuid,
            "password": password,
            "congestion_control": params.get("congestion_control", "bbr"),
            "udp_relay_mode": params.get("udp_relay_mode", "native"),
            "tls": _tls(params, host),
        }
    raise ValueError(f"unsupported scheme {scheme!r}")


def read_links() -> list[str]:
    try:
        with open(LINKS_FILE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def build(links: list[str]) -> dict:
    outbounds, tags = [], []
    for index, link in enumerate(links):
        tag = f"node-{index}"
        try:
            outbounds.append(link_to_outbound(link, tag))
            tags.append(tag)
        except Exception as exc:
            print(f"WARN: cannot parse a config: {exc}", file=sys.stderr)
    if not tags:
        raise SystemExit("no usable hysteria2/tuic links")
    # urltest keeps the fastest reachable node selected and steps aside from one
    # that stops answering, which matters more here than anywhere: these
    # protocols run over UDP and a blocked node fails silently rather than
    # refusing the connection.
    outbounds.insert(0, {
        "type": "urltest",
        "tag": "auto",
        "outbounds": tags,
        "url": TEST_URL,
        "interval": "3m",
        "tolerance": 50,
    })
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "warn"},
        "inbounds": [{
            "type": "mixed",
            "tag": "in",
            "listen": "0.0.0.0",
            "listen_port": SOCKS_PORT,
        }],
        "outbounds": outbounds,
        "route": {"final": "auto"},
    }


def main() -> None:
    destination = sys.argv[1] if len(sys.argv) > 1 else "/etc/sing-box/config.json"
    links = read_links()
    config = build(links)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    with open(destination, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    print(f"sing-box: {len(config['outbounds']) - 2} node(s) on :{SOCKS_PORT}")


if __name__ == "__main__":
    main()
