"""Refusal of links that point back into the operator's own network.

The bot fetches whatever URL it is given, and it runs inside a network that can
reach the router, the Docker host's SSH port, PostgreSQL and its own health
endpoint. Without this guard any stranger on Telegram can use the bot to read
internal services and to map the network by the difference between "connection
refused" and "TLS handshake failed".

The check resolves the hostname and refuses if *any* returned address is
private, loopback, link-local, or otherwise not routable on the public
internet — one public and one private answer for the same name is a classic
way around a first-address-only check.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Schemes that never carry a host to resolve (yt-dlp search expressions).
_HOSTLESS_SCHEMES = ("ytsearch", "ytsearchdate", "scsearch", "ytsearchall")


class BlockedAddressError(Exception):
    """The URL resolves to an address inside the operator's own network."""


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    # IPv4 tunnelled inside IPv6 (::ffff:127.0.0.1) must be judged as the IPv4
    # address it really is, or loopback slips through as "some IPv6 address".
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif getattr(ip, "sixtofour", None) is not None:
            ip = ip.sixtofour
    return ip.is_global


def resolved_addresses(host: str) -> list[str]:
    """Every address the host resolves to; the literal itself when it is an IP."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return [info[4][0] for info in infos]


def ensure_public_url(url: str) -> None:
    """Raise BlockedAddressError when the URL points into a private network.

    A name that does not resolve is left alone: it is not proof of an internal
    target, and the download will fail on its own with a clearer message. The
    same goes for search expressions, which carry no host at all.
    """
    parsed = urlparse(url)
    if parsed.scheme in _HOSTLESS_SCHEMES:
        return
    host = parsed.hostname
    if not host:
        return
    addresses = resolved_addresses(host)
    private = [a for a in addresses if not _is_public(a)]
    if private:
        raise BlockedAddressError(
            f"{host} resolves to a non-public address ({', '.join(private[:3])})"
        )
