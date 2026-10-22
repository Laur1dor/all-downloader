"""Check Telegram MTProto, retaining a pin until repeated failures prove it bad.

Called by the existing Xray refresh loop. HTTPS and SOCKS acceptance alone do
not prove that a node carries Telegram's file traffic. Candidate checks use a
separate, short-lived Xray process; the live process is never used as a probe.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import secrets
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path

try:
    from . import build_config as bc
except ImportError:  # container entry point
    import build_config as bc

TARGETS = ("149.154.167.41", "149.154.167.51", "149.154.175.50")
TIMEOUT = 4.0
FAILURES = 3
STATE = Path("/app/data/telegram_health.json")
CONFIG = Path("/etc/xray/config.json")
REBUILD = Path(os.getenv("XRAY_REBUILD_FILE", "/app/data/rebuild_xray"))
CANDIDATE_FILES = ("/app/data/tiktok_pool.txt", "/app/data/goida_live.txt")


def _exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline")
        sock.settimeout(remaining)
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise EOFError("closed")
        result.extend(chunk)
    return bytes(result)


def probe(proxy: tuple[str, int], target: str, timeout: float = TIMEOUT) -> bool:
    """A bounded SOCKS/abridged MTProto exchange, including the echoed nonce."""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection(proxy, timeout=timeout) as sock:
            def receive(size: int) -> bytes:
                return _exact(sock, size, deadline)

            sock.sendall(b"\x05\x01\x00")
            if receive(2) != b"\x05\x00":
                return False
            sock.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(target)
                         + struct.pack(">H", 443))
            head = receive(4)
            if head[:3] != b"\x05\x00\x00":
                return False
            size = {1: 4, 4: 16}.get(head[3])
            if head[3] == 3:
                size = receive(1)[0]
            if size is None:
                return False
            receive(size + 2)
            nonce = secrets.token_bytes(16)
            body = struct.pack("<I", 0xbe7e8ef1) + nonce
            message_id = int(time.time() * 2**32) // 4 * 4
            payload = b"\0" * 8 + struct.pack("<QI", message_id, len(body)) + body
            sock.sendall(b"\xef" + bytes([len(payload) // 4]) + payload)
            words = receive(1)[0]
            if words == 127:
                words = int.from_bytes(receive(3), "little")
            if not 10 <= words <= 1024:
                return False
            reply = receive(words * 4)
            return (reply[:8] == b"\0" * 8
                    and struct.unpack("<I", reply[20:24])[0] == 0x05162463
                    and reply[24:40] == nonce)
    except (OSError, EOFError, ValueError, struct.error):
        return False


def healthy(port: int) -> bool:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
        return all(pool.map(lambda target: probe(("127.0.0.1", port), target), TARGETS))


def _identity(outbound: dict) -> str:
    return json.dumps({k: v for k, v in outbound.items() if k != "tag"}, sort_keys=True)


def choose_candidate(current: dict) -> str | None:
    """Only free verified-pool inputs; never the personal/main subscription."""
    links = []
    for name in CANDIDATE_FILES:
        try:
            for link in Path(name).read_text().splitlines():
                if link.startswith("vless://") and link not in links:
                    links.append(link)
        except OSError:
            pass
    outbounds, candidates = [], []
    for link in links[:16]:
        try:
            outbound = bc.link_to_outbound(link, "candidate-" + str(len(outbounds)))
            if _identity(outbound) != _identity(current):
                candidates.append(link)
                outbounds.append(outbound)
        except Exception:
            continue  # parser errors can include credentials; never log them
    if not outbounds:
        return None
    with tempfile.TemporaryDirectory(prefix="telegram-probe-") as folder:
        # Ask the OS for available ports rather than colliding with the free
        # subscription prober's fixed range. The candidate Xray is loopback only.
        sockets = [socket.socket() for _ in outbounds]
        try:
            for sock in sockets:
                sock.bind(("127.0.0.1", 0))
            ports = [sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:
                sock.close()
        config = {"log": {"loglevel": "none"}, "inbounds": [],
                  "outbounds": outbounds, "routing": {"rules": []}}
        for port, outbound in zip(ports, outbounds):
            tag = outbound["tag"]
            config["inbounds"].append({"tag": tag, "listen": "127.0.0.1",
                                       "port": port, "protocol": "socks",
                                       "settings": {"auth": "noauth", "udp": False}})
            config["routing"]["rules"].append({"type": "field", "inboundTag": [tag],
                                                "outboundTag": tag})
        path = Path(folder) / "config.json"
        path.write_text(json.dumps(config))
        process = subprocess.Popen(["xray", "run", "-c", str(path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.3)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                passing = list(pool.map(healthy, ports))
            for link, port, passed in zip(candidates, ports, passing):
                # A single lucky reply is insufficient for replacing a live route.
                if passed and healthy(port) and healthy(port):
                    return link
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return None


def check_once() -> str:
    pin = bc._telegram_pinned_outbound()
    if pin is None:
        return "disabled"
    # Never diagnose a pin until Xray has loaded it, or overwrite an operator's
    # pending change while a configuration rebuild is in progress.
    running = json.loads(CONFIG.read_text())
    active = next((o for o in running["outbounds"] if o.get("tag") == "telegram-pinned"), None)
    if active is None or _identity(active) != _identity(pin):
        return "pending"
    import hashlib
    identity = hashlib.sha256(_identity(pin).encode()).hexdigest()
    try:
        previous = json.loads(STATE.read_text())
    except (OSError, ValueError):
        previous = {}
    good = healthy(bc.TELEGRAM_SOCKS_PORT)
    failures = 0 if good else (previous.get("failures", 0)
                              if previous.get("identity") == identity else 0) + 1
    state = {"checked_at": time.time(), "identity": identity, "healthy": good,
             "failures": failures}
    result = "healthy" if good else "unhealthy"
    if failures >= FAILURES:
        candidate = choose_candidate(pin)
        if candidate and bc._telegram_pinned_outbound() == pin:
            destination = Path(bc.TELEGRAM_NODE_FILE)
            temporary = destination.with_suffix(".next")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(candidate + "\n")
            os.replace(temporary, destination)
            REBUILD.touch()
            result = "switched"
            state["switched_at"] = time.time()
            print("telegram health: failed MTProto checks; selected verified free backup", flush=True)
        else:
            print("telegram health: no verified backup; retaining current pin", flush=True)
    STATE.write_text(json.dumps(state))
    return result


if __name__ == "__main__":
    try:
        check_once()
    except Exception as exc:
        print("telegram health: check failed (" + type(exc).__name__ + ")", flush=True)
