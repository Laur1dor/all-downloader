"""Get and keep an Instagram session without anyone copying cookies by hand.

The manual loop failed three times running: a session is exported from a
browser, works, and is dead again within a couple of weeks. Refreshing it by
hand is not a fix, it is the same chore on a timer.

This logs in from the machine that will use the session, keeps it on disk, and
renews it when it stops working. The verification code Instagram sends by mail
is read from the mailbox over IMAP, so nothing waits for a person.

Deliberately run from its own virtualenv and as a subprocess: instagrapi pulls a
large dependency tree of its own, and none of it belongs anywhere near the pins
that yt-dlp, gallery-dl and aiogram already argue over.

Configuration, all from the environment:
    IG_USERNAME, IG_PASSWORD      the account
    IG_IMAP_HOST                  e.g. imap.gmail.com
    IG_IMAP_USER, IG_IMAP_PASSWORD  mailbox that receives the codes
                                  (with 2FA on the mail account this has to be
                                  an app password, not the account password)
    IG_SESSION_FILE               default data/instagram_session.json
    IG_COOKIE_FILE                default data/cookies.txt
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

SESSION_FILE = Path(os.getenv("IG_SESSION_FILE", "data/instagram_session.json"))
COOKIE_FILE = Path(os.getenv("IG_COOKIE_FILE", "data/cookies.txt"))

_CODE_RE = re.compile(r"\b(\d{6})\b")
# Instagram sends from a handful of addresses depending on the kind of check.
_FROM_HINTS = ("instagram", "mail.instagram.com", "security@mail.instagram.com")
_CODE_WAIT_SECONDS = int(os.getenv("IG_CODE_WAIT", "180"))
_CODE_POLL_SECONDS = 10


def log(message: str) -> None:
    print(f"[iglogin] {message}", flush=True)


# --- the verification code, out of the mailbox -------------------------------

def _messages_since(box: imaplib.IMAP4_SSL, since: float) -> list[bytes]:
    """Message bodies that arrived after `since`, newest first."""
    box.select("INBOX")
    # IMAP's date search has a granularity of one day, so the day is the filter
    # and the real cut-off is applied to the parsed header below.
    day = time.strftime("%d-%b-%Y", time.gmtime(since))
    status, data = box.search(None, f'(SINCE "{day}")')
    if status != "OK":
        return []
    bodies = []
    for num in reversed(data[0].split()):
        status, payload = box.fetch(num, "(RFC822)")
        if status != "OK" or not payload or not payload[0]:
            continue
        message = email.message_from_bytes(payload[0][1])
        sender = str(message.get("From", "")).lower()
        if not any(hint in sender for hint in _FROM_HINTS):
            continue
        stamp = email.utils.parsedate_to_datetime(message.get("Date"))
        if stamp and stamp.timestamp() < since:
            continue
        bodies.append(payload[0][1])
    return bodies


def _code_from(raw: bytes) -> str | None:
    message = email.message_from_bytes(raw)
    parts = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                parts.append(part.get_payload(decode=True) or b"")
    else:
        parts.append(message.get_payload(decode=True) or b"")
    text = b"\n".join(parts).decode("utf-8", "replace")
    # Strip tags so a six-digit id inside markup cannot be mistaken for the code.
    text = re.sub(r"<[^>]+>", " ", text)
    found = _CODE_RE.findall(text)
    return found[0] if found else None


def code_from_mail(since: float) -> str | None:
    host = os.getenv("IG_IMAP_HOST", "")
    user = os.getenv("IG_IMAP_USER", "")
    password = os.getenv("IG_IMAP_PASSWORD", "")
    if not (host and user and password):
        log("no mailbox configured, cannot read the code")
        return None

    deadline = time.time() + _CODE_WAIT_SECONDS
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(host) as box:
                box.login(user, password)
                for raw in _messages_since(box, since):
                    code = _code_from(raw)
                    if code:
                        log(f"code found in the mailbox ({code[:2]}****)")
                        return code
        except Exception as exc:
            log(f"mailbox read failed: {type(exc).__name__}: {exc}")
            return None
        time.sleep(_CODE_POLL_SECONDS)
    log("no code arrived within the wait")
    return None


# --- cookies, in the format the rest of the bot already reads ----------------

def _netscape_rows(cookies: dict) -> list[str]:
    rows = []
    far_future = int(time.time()) + 365 * 24 * 3600
    for name, value in cookies.items():
        rows.append("\t".join([
            "#HttpOnly_.instagram.com" if name in ("sessionid", "ds_user_id") else ".instagram.com",
            "TRUE", "/", "TRUE", str(far_future), name, str(value),
        ]))
    return rows


def write_cookies(cookies: dict) -> int:
    """Merge the fresh Instagram cookies into the shared jar."""
    fresh = _netscape_rows(cookies)
    kept = []
    if COOKIE_FILE.exists():
        shutil.copy2(COOKIE_FILE, COOKIE_FILE.with_name(
            f"{COOKIE_FILE.name}.bak.{int(time.time())}"
        ))
        for line in COOKIE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split("\t")
            # Everything that is not Instagram is left exactly as it was.
            if len(parts) == 7 and "instagram" in parts[0].lower():
                continue
            if line.strip():
                kept.append(line)
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(
        "\n".join(["# Netscape HTTP Cookie File", *kept, *fresh]) + "\n",
        encoding="utf-8", newline="\n",
    )
    try:
        os.chmod(COOKIE_FILE, 0o666)
    except OSError:
        pass
    return len(fresh)


# --- the session itself ------------------------------------------------------

def build_client():
    from instagrapi import Client

    client = Client()
    client.delay_range = [1, 3]
    started = time.time()
    client.challenge_code_handler = lambda username, choice: code_from_mail(started - 60)
    client.change_password_handler = lambda username: None
    return client


def alive(client) -> bool:
    try:
        client.get_timeline_feed()
        return True
    except Exception as exc:
        log(f"stored session is not usable: {type(exc).__name__}")
        return False


def main() -> int:
    username = os.getenv("IG_USERNAME", "")
    password = os.getenv("IG_PASSWORD", "")
    if not (username and password):
        log("IG_USERNAME/IG_PASSWORD are not set — nothing to do")
        return 2

    client = build_client()

    if SESSION_FILE.exists():
        try:
            client.load_settings(SESSION_FILE)
            client.login(username, password)
            if alive(client):
                log("stored session still works")
                written = write_cookies(client.get_settings()["cookies"])
                log(f"refreshed {written} cookie(s) in the jar")
                return 0
        except Exception as exc:
            log(f"could not reuse the stored session: {type(exc).__name__}: {exc}")
        # A session that cannot be revived is worse than none: it makes the
        # login below reuse a device fingerprint Instagram has already refused.
        client = build_client()

    log("logging in")
    try:
        client.login(username, password)
    except Exception as exc:
        log(f"login failed: {type(exc).__name__}: {exc}")
        return 1

    if not alive(client):
        log("logged in but the session does not work")
        return 1

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    client.dump_settings(SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    written = write_cookies(client.get_settings()["cookies"])
    log(f"logged in, session stored, {written} cookie(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
