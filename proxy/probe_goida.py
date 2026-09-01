"""Отбор реально работающих нод из бесплатных подписок.

Зачем: в подписках сотни тысяч ссылок, но сквозь DPI проходит около 2%.
Класть в пул первые попавшиеся бессмысленно — балансировщик получает почти
одни трупы. Пробер крутится рядом с xray, постоянно проверяет случайную
выборку кандидатов и держит в ``goida_live.txt`` только те, через которые
реально открылся HTTPS. build_config берёт этот файл, когда он свежий.

Проверка двухступенчатая: сначала дешёвый TCP-коннект до эндпоинта (отсекает
большинство), затем настоящий прогон через одноразовый xray — SOCKS до
``generate_204``. Только вторая ступень доказывает, что нода живая.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_config as bc

LIVE_FILE = os.getenv("GOIDA_LIVE_FILE", "/app/data/goida_live.txt")
# Сколько живых нод достаточно: балансировщику хватает пары десятков, а каждая
# проверка стоит запуска xray, поэтому прекращаем, как только цель набрана.
TARGET = int(os.getenv("GOIDA_LIVE_TARGET", "25"))
# Кандидатов на круг. Из них до полной проверки доживает примерно половина.
SAMPLE = int(os.getenv("GOIDA_PROBE_SAMPLE", "700"))
# Сколько кандидатов реально прогоняем через xray за круг (это дорогая часть).
DEEP_BUDGET = int(os.getenv("GOIDA_PROBE_DEEP", "180"))
INTERVAL = int(os.getenv("GOIDA_PROBE_INTERVAL", "1800"))
# Nodes that worked at some point, kept as first-choice candidates for a while.
# A node is never blacklisted: failing a check means it was unreachable at that
# moment, which on a one-core box can simply mean the check itself was starved.
# Without this list a node that blinked once could only return by being drawn
# again out of half a million subscription links — about a 0.2% chance a round,
# so in practice never.
RECENT_FILE = os.getenv("GOIDA_RECENT_FILE", "/app/data/goida_recent.txt")
RECENT_TTL = int(os.getenv("GOIDA_RECENT_TTL", "21600"))
RECENT_MAX = int(os.getenv("GOIDA_RECENT_MAX", "300"))
TAB_CHAR = chr(9)


def stamp_line(stamp: float, link: str) -> str:
    """One line of the recent-nodes file: when it last passed, and which."""
    return f"{stamp}{TAB_CHAR}{link}" + chr(10)
# Touched when the pool changes enough to be worth reloading xray for, so the
# refresh loop can act at once instead of sitting out its whole interval.
REBUILD_FILE = os.getenv("XRAY_REBUILD_FILE", "/app/data/rebuild_xray")
# TikTok gets its own verified pool: a node that carries plain HTTPS often still
# cannot fetch TikTok, and the free ones stop working minutes after they start.
TIKTOK_POOL_FILE = os.getenv("TIKTOK_POOL_FILE", "/app/data/tiktok_pool.txt")
TIKTOK_POOL_TARGET = int(os.getenv("TIKTOK_POOL_TARGET", "8"))
# Re-verified far more often than the main round: the pool is only useful while
# its nodes are alive, and they die fast.
# The ladder is rebuilt with the live list; nothing here talks to TikTok,
# so there is no reason to run it on a tighter timer of its own.
TIKTOK_RECHECK_INTERVAL = int(os.getenv("TIKTOK_RECHECK_INTERVAL", "600"))
# The bot creates this when a TikTok download fails, to force a check right away
# instead of waiting out the interval.
TIKTOK_TRIGGER_FILE = os.getenv("TIKTOK_TRIGGER_FILE", "/app/data/reprobe_tiktok")
TIKTOK_HOST = "www.tiktok.com"
# A video page, not a profile: a node can serve the profile and still fail on
# the video, which is what the bot actually asks for. The check has to be the
# same shape as the real work or the pool fills with nodes that pass it and then
# fail every download.
TIKTOK_PATH = os.getenv("TIKTOK_PROBE_PATH", "/@tiktok/video/7106594312292453675")
_CRLF = chr(13) + chr(10)
TCP_WORKERS = int(os.getenv("GOIDA_PROBE_TCP_WORKERS", "60"))
# Параллельных xray немного: у сервера одно ядро.
DEEP_WORKERS = int(os.getenv("GOIDA_PROBE_DEEP_WORKERS", "6"))
BASE_PORT = 41000

_ENDPOINT_RE = re.compile(r"vless://[^@]+@([^:/?#]+):(\d+)")


def _endpoint(link: str) -> tuple[str, int] | None:
    m = _ENDPOINT_RE.match(link)
    return (m.group(1), int(m.group(2))) if m else None


def _candidates() -> list[str]:
    """Случайная выборка ссылок из всех подписок.

    Именно случайная, а не первые N: начало файлов у всех одинаковое и уже
    затёрто нагрузкой, а подписки обновляются каждые 5–30 минут.
    """
    urls = [s.strip() for s in os.getenv("GOIDA_SUBSCRIPTIONS", "").split(",") if s.strip()]
    links: list[str] = []
    seen: set[str] = set()

    def fetch(url: str) -> list[str]:
        try:
            text = bc._fetch_first(url)
        except Exception as exc:
            print(f"probe: подписка {url!r} недоступна: {exc}", flush=True)
            return []
        return [
            ln.strip() for ln in text.splitlines()
            if ln.strip().startswith("vless://") and not bc._is_russian(ln.strip())
        ]

    with cf.ThreadPoolExecutor(min(12, len(urls) or 1)) as pool:
        per_source = list(pool.map(fetch, urls))

    # Из каждого источника берём поровну, чтобы один гигантский блок не вытеснил
    # остальные — у разных агрегаторов разные ноды и разная выживаемость.
    quota = max(1, SAMPLE // max(1, len([p for p in per_source if p])))
    for source in per_source:
        for link in random.sample(source, min(quota, len(source))):
            if link not in seen:
                seen.add(link)
                links.append(link)
    random.shuffle(links)
    return links


def _tcp_alive(link: str) -> str | None:
    endpoint = _endpoint(link)
    if not endpoint:
        return None
    try:
        socket.create_connection(endpoint, timeout=6).close()
        return link
    except OSError:
        return None


def _really_works(args: tuple[int, str]) -> str | None:
    """Поднять одноразовый xray на этой ноде и попробовать открыть HTTPS."""
    port, link = args
    try:
        outbounds, _ = bc._links_to_outbounds([link], "probe-")
    except Exception:
        return None
    if not outbounds:
        return None
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{
            "tag": "in", "protocol": "socks", "port": port, "listen": "127.0.0.1",
            "settings": {"udp": False},
        }],
        "outbounds": outbounds,
    }
    path = f"/tmp/probe-{port}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    proc = subprocess.Popen(
        ["xray", "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    ok = False
    try:
        time.sleep(2)
        sock = socket.create_connection(("127.0.0.1", port), timeout=12)
        sock.sendall(b"\x05\x01\x00")
        sock.recv(2)
        host = b"www.google.com"
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack("!H", 443))
        reply = sock.recv(10)
        if len(reply) >= 2 and reply[1] == 0:
            tls = ssl.create_default_context().wrap_socket(sock, server_hostname="www.google.com")
            tls.sendall(
                b"GET /generate_204 HTTP/1.1\r\nHost: www.google.com\r\nConnection: close\r\n\r\n"
            )
            ok = b"204" in tls.recv(40)
    except Exception:
        pass
    finally:
        proc.kill()
        proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass
    return link if ok else None



def _read_recent() -> list[str]:
    """Links that passed a check recently, newest first, expired ones dropped."""
    now = time.time()
    out: list[tuple[float, str]] = []
    try:
        with open(RECENT_FILE, encoding="utf-8") as fh:
            for line in fh:
                stamp, _, link = line.strip().partition(TAB_CHAR)
                if not link.startswith("vless://"):
                    continue
                try:
                    seen = float(stamp)
                except ValueError:
                    continue
                if now - seen <= RECENT_TTL:
                    out.append((seen, link))
    except OSError:
        return []
    out.sort(reverse=True)
    return [link for _, link in out]


def _remember_recent(live: list[str]) -> None:
    """Refresh the timestamps of nodes that just passed, keep the rest."""
    now = time.time()
    seen_now = set(live)
    entries = [(now, link) for link in live]
    for link in _read_recent():
        if link not in seen_now:
            entries.append((now - RECENT_TTL / 2, link))
    entries = entries[:RECENT_MAX]
    try:
        os.makedirs(os.path.dirname(RECENT_FILE) or ".", exist_ok=True)
        tmp = f"{RECENT_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for stamp, link in entries:
                fh.write(stamp_line(stamp, link))
        os.replace(tmp, RECENT_FILE)
    except OSError as exc:
        print(f"probe: could not write {RECENT_FILE}: {exc}", flush=True)


def _request_rebuild() -> None:
    """Ask the refresh loop to rebuild the xray config now."""
    try:
        os.makedirs(os.path.dirname(REBUILD_FILE) or ".", exist_ok=True)
        with open(REBUILD_FILE, "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError:
        pass


def _read_live() -> list[str]:
    try:
        with open(LIVE_FILE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    except OSError:
        return []


def _write_live(links: list[str]) -> None:
    os.makedirs(os.path.dirname(LIVE_FILE) or ".", exist_ok=True)
    tmp = f"{LIVE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(links) + "\n")
    os.replace(tmp, LIVE_FILE)


def probe_round() -> list[str]:
    started = time.time()
    # Ноды, найденные в прошлый раз, проверяем первыми: они уже доказали, что
    # проходят DPI, и чаще всего живы до сих пор — так набор не скачет каждый круг.
    previous = _read_live()
    # Order matters: the deep check has a budget, and nodes that worked before
    # are far likelier to work again than a fresh draw from the subscriptions.
    known = previous + [c for c in _read_recent() if c not in set(previous)]
    candidates = known + [c for c in _candidates() if c not in set(known)]
    if not candidates:
        print("probe: кандидатов нет — подписки недоступны", flush=True)
        return previous

    with cf.ThreadPoolExecutor(TCP_WORKERS) as pool:
        reachable = [link for link in pool.map(_tcp_alive, candidates) if link]
    print(
        f"probe: кандидатов {len(candidates)}, TCP-доступны {len(reachable)} "
        f"({time.time() - started:.0f}с)",
        flush=True,
    )

    live: list[str] = []
    batch = reachable[:DEEP_BUDGET]
    with cf.ThreadPoolExecutor(DEEP_WORKERS) as pool:
        for link in pool.map(
            _really_works, [(BASE_PORT + i, c) for i, c in enumerate(batch)]
        ):
            if link:
                live.append(link)
                if len(live) >= TARGET:
                    break

    if live:
        _write_live(live)
        _remember_recent(live)
        if len(live) != len(previous):
            _request_rebuild()
        print(
            f"probe: живых {len(live)}/{len(batch)} → {LIVE_FILE} "
            f"(круг занял {time.time() - started:.0f}с)",
            flush=True,
        )
    else:
        # Пустой результат не затираем: старый набор всё ещё лучше, чем ничего.
        print(
            f"probe: живых 0/{len(batch)} — оставляю прошлые {len(previous)} нод",
            flush=True,
        )
    return live or previous


def _https_get_via_socks(port: int, host: str, path: str, timeout: int = 20) -> bytes:
    """One HTTPS GET through a local SOCKS5 port. Empty bytes on any failure."""
    request = (
        f"GET {path} HTTP/1.1|Host: {host}|"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36|"
        "Accept: text/html|Connection: close||"
    ).replace("|", _CRLF).encode()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return b""
    try:
        sock.sendall(bytes([5, 1, 0]))
        sock.recv(2)
        target = host.encode()
        sock.sendall(bytes([5, 1, 0, 3]) + bytes([len(target)]) + target + struct.pack("!H", 443))
        reply = sock.recv(10)
        if len(reply) < 2 or reply[1] != 0:
            return b""
        tls = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        tls.sendall(request)
        chunks, total = [], 0
        while total < 400_000:
            part = tls.recv(65536)
            if not part:
                break
            chunks.append(part)
            total += len(part)
        return b"".join(chunks)
    except Exception:
        return b""
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _read_tiktok_pool() -> list[str]:
    try:
        with open(TIKTOK_POOL_FILE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    except OSError:
        return []


def refresh_tiktok_pool(live: list[str]) -> None:
    """Fill the TikTok ladder from nodes already known to be alive.

    There used to be a TikTok-specific check here, and it was doing harm. TikTok
    allows an address only a handful of requests before it starts answering with
    a 536-byte "Site Maintenance" page, and the check spent that allowance every
    few minutes: nodes passed, were written to the pool, and were already
    exhausted by the time a real download used them. Measured afterwards, all
    five pool nodes served stubs while an untouched node still returned the real
    page.

    So nothing is spent on qualifying a node for TikTok. The ladder is simply the
    live nodes, and the download itself moves to the next rung when one refuses —
    the retry is the test, and it is the only test that costs nothing extra.
    """
    seen: set[str] = set()
    chosen: list[str] = []
    for link in bc.collect_bypass_links() + live:
        endpoint = _endpoint(link)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        chosen.append(link)
        if len(chosen) >= TIKTOK_POOL_TARGET:
            break
    if not chosen:
        print("probe: no live nodes for the TikTok ladder", flush=True)
        return
    previous = _read_tiktok_pool()
    os.makedirs(os.path.dirname(TIKTOK_POOL_FILE) or ".", exist_ok=True)
    tmp = f"{TIKTOK_POOL_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(chr(10).join(chosen) + chr(10))
    os.replace(tmp, TIKTOK_POOL_FILE)
    if set(chosen) != set(previous):
        _request_rebuild()
    print(f"probe: tiktok ladder {len(chosen)} node(s)", flush=True)


def _wait_or_trigger(seconds: int) -> None:
    """Sleep, but wake early when the bot asks for a re-check."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.exists(TIKTOK_TRIGGER_FILE):
            try:
                os.unlink(TIKTOK_TRIGGER_FILE)
            except OSError:
                pass
            print("probe: re-check requested by the bot", flush=True)
            return
        time.sleep(5)


def main() -> None:
    once = "--once" in sys.argv
    live: list[str] = _read_live()
    next_full_round = 0.0
    while True:
        try:
            # TikTok first, always. The full sweep takes minutes — it was timed at
            # 391 s — and running it first left the TikTok pool empty for that
            # whole stretch after a restart, which is exactly when downloads fail.
            # The pool tops up from the live list already read from disk, so it
            # does not need the sweep to have run.
            refresh_tiktok_pool(live)
            # The sweep is expensive and its results change slowly, so it keeps
            # its own, much longer clock.
            if time.time() >= next_full_round:
                live = probe_round() or live
                next_full_round = time.time() + INTERVAL
                # Fresh candidates just arrived; give TikTok first pick of them.
                refresh_tiktok_pool(live)
        except Exception as exc:  # the prober must not die with xray
            print(f"probe: round failed: {exc}", flush=True)
        if once:
            return
        _wait_or_trigger(TIKTOK_RECHECK_INTERVAL)


if __name__ == "__main__":
    main()
