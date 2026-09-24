"""Put the server's browser in front of a person, so they can answer a CAPTCHA.

Instagram sometimes stops this account with /auth_platform/recaptcha/ on every
page. A CAPTCHA is for a person, and nothing here tries to answer one - but the
session that has to answer it lives in this container's browser profile, not on
anybody's phone, so signing in elsewhere does not reliably clear it.

So on request this starts that browser on a virtual screen, puts the screen on
the network behind noVNC with a one-off password, and waits. The operator opens
the link, answers the CAPTCHA themselves, and the moment Instagram stops showing
it this stores the session and takes everything down again. Nothing is listening
between sessions, and a session that nobody opens closes itself.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/scripts")

REQUEST_FILE = Path(os.getenv("IG_REMOTE_REQUEST", "/app/data/igremote_request"))
STATUS_FILE = Path(os.getenv("IG_REMOTE_STATUS", "/app/data/igremote_status.json"))
# While this exists the browser profile belongs to a person; the resolver and
# the login leave it alone rather than fight over the profile lock.
ACTIVE_FILE = Path(os.getenv("IG_REMOTE_ACTIVE", "/app/data/igremote.active"))
PROFILE_DIR = Path(os.getenv("IG_PROFILE_DIR", "/app/data/ig-profile"))
WEB_PORT = int(os.getenv("IG_REMOTE_PORT", "6080"))
VNC_PORT = 5900
DISPLAY = ":99"
# Portrait-ish, because the screen it will be looked at on is a phone.
SCREEN = os.getenv("IG_REMOTE_SCREEN", "900x1200")
LIFETIME = int(os.getenv("IG_REMOTE_LIFETIME", "1200"))
_CHALLENGE_MARKS = ("/auth_platform/", "/challenge/")


def log(message: str) -> None:
    print(f"[igremote] {message}", flush=True)


def _status(**fields) -> None:
    fields["at"] = int(time.time())
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(fields), encoding="utf-8")
    try:
        os.chmod(STATUS_FILE, 0o666)
    except OSError:
        pass


def _challenged(url: str) -> bool:
    return any(mark in (url or "") for mark in _CHALLENGE_MARKS)


def session() -> None:
    from playwright.sync_api import sync_playwright

    from iglogin_browser import _logged_in, write_cookies

    password = secrets.token_urlsafe(6)[:8]
    width, height = SCREEN.split("x")
    procs: list[subprocess.Popen] = []
    ACTIVE_FILE.write_text(str(int(time.time())), encoding="utf-8")
    try:
        procs.append(subprocess.Popen(
            ["Xvfb", DISPLAY, "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        time.sleep(1.5)
        # The VNC server answers only inside the container; the one thing on
        # the network is the web bridge, and it needs the password.
        procs.append(subprocess.Popen(
            ["x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT), "-localhost",
             "-passwd", password, "-forever", "-shared", "-quiet", "-noxdamage"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        procs.append(subprocess.Popen(
            ["websockify", "--web", "/usr/share/novnc", str(WEB_PORT),
             f"localhost:{VNC_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        time.sleep(1.5)

        env_display = {"DISPLAY": DISPLAY}
        with sync_playwright() as driver:
            context = driver.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=False,
                env={**os.environ, **env_display},
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    f"--window-size={width},{height}",
                    "--window-position=0,0",
                ],
                no_viewport=True,
                locale="en-US",
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://www.instagram.com/", timeout=60000)
                page.wait_for_timeout(4000)
                was_challenged = _challenged(page.url)
                _status(state="open", password=password, port=WEB_PORT,
                        challenged=was_challenged, url=page.url.split("?")[0])
                log(f"open on :{WEB_PORT}, challenged={was_challenged}")

                deadline = time.time() + LIFETIME
                while time.time() < deadline:
                    page.wait_for_timeout(3000)
                    try:
                        url = page.url
                    except Exception:
                        # The operator closed the tab; open a fresh one.
                        page = context.new_page()
                        page.goto("https://www.instagram.com/", timeout=60000)
                        continue
                    if _challenged(url):
                        continue
                    # Out of the challenge. Give the site a moment to land,
                    # then believe the session only if Instagram does.
                    page.wait_for_timeout(5000)
                    if _challenged(page.url):
                        continue
                    if _logged_in(page):
                        written = write_cookies(context.cookies())
                        _status(state="signed_in", cookies=written)
                        log(f"cleared and signed in, {written} cookie(s) stored")
                        return
                    # Past the CAPTCHA but not signed in: the login can finish
                    # it, and it needs this profile back to do so.
                    _status(state="cleared_not_signed_in")
                    log("cleared, but the session is not signed in")
                    return

                _status(state="expired")
                log("nobody finished it in time; closing")
            finally:
                context.close()
    except Exception as exc:
        _status(state="failed", error=f"{type(exc).__name__}: {exc}"[:200])
        log(f"failed: {type(exc).__name__}: {exc}")
    finally:
        for proc in reversed(procs):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            ACTIVE_FILE.unlink()
        except OSError:
            pass


def main() -> int:
    # A session cut short by a restart must not leave the profile marked busy.
    try:
        ACTIVE_FILE.unlink()
    except OSError:
        pass
    log(f"waiting for a request at {REQUEST_FILE}")
    while True:
        if REQUEST_FILE.exists():
            try:
                REQUEST_FILE.unlink()
            except OSError:
                pass
            log("request received")
            _status(state="starting")
            session()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
