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
# TikTok gets its own verified pool: a node that carries plain HTTPS often still
# cannot fetch TikTok, and the free ones stop working minutes after they start.
TIKTOK_POOL_FILE = os.getenv("TIKTOK_POOL_FILE", "/app/data/tiktok_pool.txt")
TIKTOK_POOL_TARGET = int(os.getenv("TIKTOK_POOL_TARGET", "5"))
TIKTOK_POOL_CANDIDATES = int(os.getenv("TIKTOK_POOL_CANDIDATES", "20"))
# Re-verified far more often than the main round: the pool is only useful while
# its nodes are alive, and they die fast.
TIKTOK_RECHECK_INTERVAL = int(os.getenv("TIKTOK_RECHECK_INTERVAL", "240"))
# The bot creates this when a TikTok download fails, to force a check right away
# instead of waiting out the interval.
TIKTOK_TRIGGER_FILE = os.getenv("TIKTOK_TRIGGER_FILE", "/app/data/reprobe_tiktok")
TIKTOK_HOST = "www.tiktok.com"
# A video page, not a profile: a node can serve the profile and still fail on
# the video, which is what the bot actually asks for. The check has to be the
# same shape as the real work or the pool fills with nodes that pass it and then
# fail every download.
TIKTOK_PATH = os.getenv("TIKTOK_PROBE_PATH", "/@tiktok/video/7106594312292453675")
# The marker yt-dlp itself needs; a block page or a captcha carries neither.
TIKTOK_MARKER = b"__UNIVERSAL_DATA_FOR_REHYDRATION__"
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
    candidates = previous + [c for c in _candidates() if c not in set(previous)]
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


def _serves_tiktok(args: tuple[int, str]) -> str | None:
    """Whether this one node can fetch a real TikTok page, not a block page."""
    port, link = args
    try:
        outbounds, _ = bc._links_to_outbounds([link], "ttprobe-")
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
    path = f"/tmp/tt-{port}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh)
    proc = subprocess.Popen(
        ["xray", "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        time.sleep(2)
        body = _https_get_via_socks(port, TIKTOK_HOST, TIKTOK_PATH)
        # "blocked from accessing" comes back with a 200 and the marker present,
        # so the marker alone is not enough to call the node good.
        if TIKTOK_MARKER not in body or b"blocked from accessing" in body:
            return None
        return link
    finally:
        proc.kill()
        proc.wait()
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_tiktok_pool() -> list[str]:
    try:
        with open(TIKTOK_POOL_FILE, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip().startswith("vless://")]
    except OSError:
        return []


def refresh_tiktok_pool(live: list[str]) -> list[str]:
    """Re-verify the TikTok pool, topping it up from the freshest candidates.

    Nodes already in the pool are checked first and kept while they answer, so
    the exit does not change under a download that is in flight. Only the empty
    slots are filled, and curated nodes get first refusal because they outlive
    the free ones by a wide margin.
    """
    current = _read_tiktok_pool()
    with cf.ThreadPoolExecutor(min(4, max(1, len(current)))) as pool:
        survivors = [
            link for link in pool.map(
                _serves_tiktok, [(44000 + i, c) for i, c in enumerate(current)]
            ) if link
        ]
    dropped = len(current) - len(survivors)

    if len(survivors) < TIKTOK_POOL_TARGET:
        seen = set(survivors)
        candidates = [
            c for c in bc.collect_bypass_links() + live if c not in seen
        ][:TIKTOK_POOL_CANDIDATES]
        with cf.ThreadPoolExecutor(4) as pool:
            for link in pool.map(
                _serves_tiktok, [(44100 + i, c) for i, c in enumerate(candidates)]
            ):
                if link:
                    survivors.append(link)
                    if len(survivors) >= TIKTOK_POOL_TARGET:
                        break

    if survivors:
        os.makedirs(os.path.dirname(TIKTOK_POOL_FILE) or ".", exist_ok=True)
        tmp = f"{TIKTOK_POOL_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(chr(10).join(survivors) + chr(10))
        os.replace(tmp, TIKTOK_POOL_FILE)
    print(f"probe: tiktok pool {len(survivors)} node(s), {dropped} dropped", flush=True)
    return survivors


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
            # The full sweep is expensive; the TikTok pool is cheap and has to be
            # checked often, so they run on separate clocks.
            if time.time() >= next_full_round:
                live = probe_round() or live
                next_full_round = time.time() + INTERVAL
            refresh_tiktok_pool(live)
        except Exception as exc:  # the prober must not die with xray
            print(f"probe: round failed: {exc}", flush=True)
        if once:
            return
        _wait_or_trigger(TIKTOK_RECHECK_INTERVAL)


if __name__ == "__main__":
    main()
