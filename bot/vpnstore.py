"""Accepting VPN configurations from the admin chat and swapping them live.

Replacing an exit used to mean editing ``.env`` over SSH and rebuilding the
proxy containers, which is exactly the wrong shape for the thing it fixes: an
exit is replaced *because* it just died, usually while away from the machine.
The admin sends the new config to the bot instead — as a link, a subscription
address, a ``.conf`` or a JSON file — and it takes effect within the poll
interval of whichever engine carries it.

Nothing is executed here. Each engine owns its own reload:

  * xray reads ``data/vpn/xray.txt`` and ``data/vpn/subs.txt`` and rebuilds when
    ``data/rebuild_xray`` appears;
  * the AmneziaWG container watches ``data/awg/<name>.conf`` and
    ``data/vpn/reload_awg``;
  * sing-box (hysteria2, tuic — protocols xray does not speak) watches
    ``data/vpn/singbox.txt`` and ``data/vpn/reload_singbox``.

The bot therefore never needs the docker socket, which would hand every user of
this container root on the host.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

VPN_DIR = Path(os.getenv("VPN_DIR", "data/vpn"))
AWG_DIR = Path(os.getenv("AWG_CONFIG_DIR", "data/awg"))
XRAY_LINKS = VPN_DIR / "xray.txt"
SUBSCRIPTIONS = VPN_DIR / "subs.txt"
SINGBOX_LINKS = VPN_DIR / "singbox.txt"
REBUILD_XRAY = Path(os.getenv("XRAY_REBUILD_FILE", "data/rebuild_xray"))
RELOAD_AWG = VPN_DIR / "reload_awg"
RELOAD_SINGBOX = VPN_DIR / "reload_singbox"

# Protocols xray-core carries, and the ones it does not. Splitting the input by
# scheme is what lets a single "here, use this" message work regardless of which
# engine has to run it.
XRAY_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://")
SINGBOX_SCHEMES = ("hysteria2://", "hy2://", "tuic://", "hysteria://")

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_LINKS = 200


@dataclass
class Payload:
    """What an admin message turned out to contain."""

    xray: list[str] = field(default_factory=list)
    singbox: list[str] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    awg: str | None = None
    awg_name: str = "awg0"
    xray_json: str | None = None
    unknown: int = 0

    def __bool__(self) -> bool:
        return bool(
            self.xray or self.singbox or self.subscriptions
            or self.awg or self.xray_json
        )


def _b64_maybe(text: str) -> str:
    """Subscription bodies come either as a link list or as base64 of one."""
    stripped = "".join(text.split())
    if "://" in text:
        return text
    try:
        return base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode(
            "utf-8", "replace"
        )
    except (binascii.Error, ValueError):
        return text


def safe_name(raw: str) -> str:
    """A filename the operator will recognise, with nothing that escapes a path."""
    name = _NAME_SAFE.sub("-", (raw or "").strip()).strip("-.")
    name = name.removesuffix(".conf")
    return name[:40] or "awg0"


def classify(text: str, filename: str = "") -> Payload:
    """Read whatever the admin sent and sort it by the engine that can run it.

    One message may carry several kinds at once — a subscription plus a couple
    of spare links is a normal thing to paste — so this collects rather than
    picks.
    """
    payload = Payload()
    body = (text or "").strip()
    if not body:
        return payload

    lowered = body.lower()
    if "[interface]" in lowered and "privatekey" in lowered:
        payload.awg = body
        payload.awg_name = safe_name(filename or "awg0")
        return payload

    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except ValueError:
            payload.unknown += 1
            return payload
        if "outbounds" in parsed:
            payload.xray_json = json.dumps(parsed, indent=2)
            return payload
        payload.unknown += 1
        return payload

    # A bare base64 blob is how most subscription bodies arrive when pasted.
    if "://" not in body:
        decoded = _b64_maybe(body)
        if "://" in decoded:
            body = decoded

    for line in body.replace(",", "\n").splitlines():
        link = line.strip()
        if not link or link.startswith("#"):
            continue
        low = link.lower()
        if low.startswith(XRAY_SCHEMES):
            payload.xray.append(link)
        elif low.startswith(SINGBOX_SCHEMES):
            payload.singbox.append(link)
        elif low.startswith(("http://", "https://")):
            payload.subscriptions.append(link)
        else:
            payload.unknown += 1

    for bucket in (payload.xray, payload.singbox, payload.subscriptions):
        del bucket[_MAX_LINKS:]
    return payload


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    # A rename is atomic, so an engine reading the file mid-write never sees a
    # half-written config and drops the whole pool over it.
    tmp.replace(path)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def _merge(path: Path, links: list[str], replace: bool) -> list[str]:
    existing: list[str] = []
    if not replace and path.is_file():
        existing = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
    merged = existing + [ln for ln in links if ln not in existing]
    del merged[_MAX_LINKS:]
    _write(path, "\n".join(merged) + "\n")
    return merged


def apply_payload(payload: Payload, replace: bool = True) -> list[str]:
    """Store the configs and ask the affected engines to pick them up.

    Returns one human line per engine that was touched. ``replace`` swaps the
    stored set outright, which is the usual intent — the previous exit is dead —
    while False appends, for building a pool out of several messages.
    """
    report: list[str] = []

    if payload.awg:
        conf = AWG_DIR / f"{payload.awg_name}.conf"
        # Written without the carriage returns Windows editors add: the tunnel
        # setup reads these fields with plain text tools.
        _write(conf, payload.awg.replace("\r\n", "\n"))
        _touch(RELOAD_AWG)
        report.append(f"🔐 AmneziaWG: <code>{payload.awg_name}.conf</code> — туннель поднимется заново")

    if payload.xray_json:
        _write(VPN_DIR / "xray-custom.json", payload.xray_json)
        _touch(REBUILD_XRAY)
        report.append("📄 Готовый конфиг xray сохранён")

    if payload.xray:
        merged = _merge(XRAY_LINKS, payload.xray, replace)
        _touch(REBUILD_XRAY)
        report.append(f"🌐 xray: {len(payload.xray)} шт. принято, всего {len(merged)}")

    if payload.subscriptions:
        merged = _merge(SUBSCRIPTIONS, payload.subscriptions, replace)
        _touch(REBUILD_XRAY)
        report.append(f"🔗 Подписки: {len(payload.subscriptions)} шт., всего {len(merged)}")

    if payload.singbox:
        merged = _merge(SINGBOX_LINKS, payload.singbox, replace)
        _touch(RELOAD_SINGBOX)
        report.append(f"⚡ sing-box (hysteria2/tuic): {len(payload.singbox)} шт., всего {len(merged)}")

    return report


def _count(path: Path) -> int:
    try:
        return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])
    except OSError:
        return 0


def _hosts(path: Path, limit: int = 3) -> list[str]:
    names = []
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return names
    for link in lines[:limit]:
        parsed = urlparse(link)
        names.append(parsed.hostname or link[:24])
    return names


def describe() -> str:
    """What is loaded right now, for the control panel."""
    awg = sorted(p.stem for p in AWG_DIR.glob("*.conf")) if AWG_DIR.is_dir() else []
    lines = [
        f"🌐 xray-ссылки: <b>{_count(XRAY_LINKS)}</b>"
        + (f" ({', '.join(_hosts(XRAY_LINKS))}…)" if _count(XRAY_LINKS) else ""),
        f"🔗 Подписки: <b>{_count(SUBSCRIPTIONS)}</b>",
        f"⚡ hysteria2/tuic: <b>{_count(SINGBOX_LINKS)}</b>",
        f"🔐 AmneziaWG: <b>{', '.join(awg) or 'нет'}</b>",
    ]
    return "\n".join(lines)
