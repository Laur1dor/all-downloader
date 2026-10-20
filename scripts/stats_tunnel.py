"""Keep a Cloudflare statistics tunnel alive only during owner-requested windows."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path

_NAME = "stats-tunnel"
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def read_window(path: Path) -> float:
    try:
        value = float(json.loads(path.read_text(encoding="utf-8"))["expires_at"])
        return value if 0 < value - time.time() <= 7205 else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def main() -> None:
    root = Path(os.getenv("STATS_TUNNEL_DATA", "data"))
    root.mkdir(parents=True, exist_ok=True)
    request = root / "stats_tunnel_request.json"
    status = root / "stats_tunnel_status.json"
    image = os.getenv("STATS_TUNNEL_IMAGE", "cloudflare/cloudflared:latest")
    network = os.getenv("STATS_TUNNEL_NETWORK_CONTAINER", "tun2socks")
    origin = os.getenv("STATS_TUNNEL_ORIGIN", "http://bot:30080")
    process = None
    poller = selectors.DefaultSelector()
    alive, ready, candidate, retry_after, began = True, False, None, 0, 0
    buffered = ""
    last_status = None

    def stop(*_args):
        nonlocal alive
        alive = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def publish(state: str, until: float = 0, url: str | None = None):
        nonlocal last_status
        value = {"state": state, "expires_at": until, "url": url}
        if value == last_status:
            return
        temporary = status.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(status)
        last_status = value

    def remove():
        nonlocal process, ready, candidate, buffered
        if process is not None:
            if process.stdout is not None:
                poller.unregister(process.stdout)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stdout is not None:
                process.stdout.close()
            process = None
        subprocess.run(["docker", "rm", "-f", _NAME], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False)
        ready, candidate = False, None
        buffered = ""

    remove()
    publish("closed")
    try:
        while alive:
            until = read_window(request)
            now = time.time()
            if not until:
                if process is not None:
                    remove()
                    publish("closed")
            elif process is None and now >= retry_after:
                publish("starting", until)
                process = subprocess.Popen(
                    ["docker", "run", "--rm", "--pull=never", "--name", _NAME,
                     "--network", "container:" + network, "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", image, "tunnel", "--no-autoupdate",
                     "--protocol", "http2", "--url", origin],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                os.set_blocking(process.stdout.fileno(), False)
                poller.register(process.stdout, selectors.EVENT_READ)
                began = now
            if process is not None:
                for key, _ in poller.select(timeout=1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    buffered += chunk.decode("utf-8", errors="replace")
                    lines = buffered.split("\n")
                    buffered = lines.pop()[-8192:]
                    for line in lines:
                        found = _URL.search(line)
                        if found:
                            candidate = found[0]
                        if candidate and "Registered tunnel connection" in line:
                            ready = True
                if ready:
                    publish("ready", until, candidate)
                if process.poll() is not None or (not ready and now - began > 50):
                    remove()
                    publish("unavailable", until)
                    retry_after = time.time() + 60
            else:
                time.sleep(1)
    finally:
        remove()
        publish("closed")
        poller.close()


if __name__ == "__main__":
    main()
