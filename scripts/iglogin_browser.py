"""Log into Instagram with a real browser, on the machine that uses the session.

Two things forced this, and both were measured rather than assumed.

The mobile API refuses every app version it does not recognise — instagrapi's
current release included — so the library route is a dead end. And the web login
now answers a new device with `checkpoint_required` pointing at /auth_platform/,
which is a 537 KB Bloks page: the old JSON challenge API that scripts used to
answer is gone. A checkpoint that only renders in a browser has to be answered
in a browser.

A browser also solves the other half. The session cookie is HttpOnly, so no
amount of JavaScript in a page will read it out — but a browser being driven
hands its whole cookie jar over, HttpOnly included. That is the thing the manual
loop existed to copy by hand, three times over.

The profile is kept between runs, so Instagram remembers the device and the
checkpoint is asked once rather than every time. The code it mails is read from
the mailbox over IMAP, so nothing waits for a person.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import shutil
import sys
import time
from pathlib import Path

PROFILE_DIR = Path(os.getenv("IG_PROFILE_DIR", "/app/data/ig-profile"))
COOKIE_FILE = Path(os.getenv("IG_COOKIE_FILE", "/app/data/cookies.txt"))
RESULT_FILE = Path(os.getenv("IG_RESULT_FILE", "/app/data/iglogin_result.txt"))

_CODE_RE = re.compile(r"\b(\d{6})\b")
_FROM_HINTS = ("instagram", "facebookmail", "mail.instagram.com")
_CODE_WAIT_SECONDS = int(os.getenv("IG_CODE_WAIT", "180"))
_CODE_POLL_SECONDS = 10

_lines: list[str] = []


def log(message: str) -> None:
    print(f"[iglogin] {message}", flush=True)
    _lines.append(message)


def report(ok: bool) -> None:
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = ("OK" if ok else "FAIL") + "\n" + "\n".join(_lines[-15:])
    RESULT_FILE.write_text(body, encoding="utf-8")


# --- the verification code, out of the mailbox -------------------------------

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
    # Tags stripped, so a six-digit id inside markup cannot pass for the code.
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
                box.select("INBOX")
                day = time.strftime("%d-%b-%Y", time.gmtime(since))
                status, data = box.search(None, f'(SINCE "{day}")')
                if status == "OK" and data and data[0]:
                    for num in reversed(data[0].split()[-40:]):
                        status, payload = box.fetch(num, "(RFC822)")
                        if status != "OK" or not payload or not payload[0]:
                            continue
                        raw = payload[0][1]
                        parsed = email.message_from_bytes(raw)
                        sender = str(parsed.get("From", "")).lower()
                        if not any(hint in sender for hint in _FROM_HINTS):
                            continue
                        stamp = email.utils.parsedate_to_datetime(parsed.get("Date"))
                        if stamp and stamp.timestamp() < since:
                            continue
                        code = _code_from(raw)
                        if code:
                            log("code found in the mailbox")
                            return code
        except Exception as exc:
            log(f"mailbox read failed: {type(exc).__name__}: {exc}")
            return None
        log("code has not arrived yet, waiting")
        time.sleep(_CODE_POLL_SECONDS)
    log("no code arrived within the wait")
    return None


# --- cookies, in the format the rest of the bot already reads ----------------

def write_cookies(cookies: list[dict]) -> int:
    rows = []
    far_future = int(time.time()) + 365 * 24 * 3600
    for cookie in cookies:
        if "instagram" not in (cookie.get("domain") or ""):
            continue
        expiry = int(cookie.get("expires") or 0)
        rows.append("\t".join([
            ("#HttpOnly_" if cookie.get("httpOnly") else "") + ".instagram.com",
            "TRUE",
            cookie.get("path") or "/",
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expiry if expiry > 0 else far_future),
            cookie["name"],
            cookie["value"],
        ]))
    if not rows:
        return 0

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
        "\n".join(["# Netscape HTTP Cookie File", *kept, *rows]) + "\n",
        encoding="utf-8", newline="\n",
    )
    try:
        os.chmod(COOKIE_FILE, 0o666)
    except OSError:
        pass
    return len(rows)


# --- the browser -------------------------------------------------------------

# Instagram serves more than one spelling of its own login form: a browser here
# was given inputs named email/pass where the documented ones are
# username/password. Both are accepted rather than one being guessed at.
_USER_FIELDS = ('input[name="username"]', 'input[name="email"]')
_PASS_FIELDS = ('input[name="password"]', 'input[name="pass"]')


def _first_visible(page, selectors):
    for selector in selectors:
        box = page.locator(selector).first
        try:
            if box.count() and box.is_visible():
                return box
        except Exception:
            continue
    return None


def _describe(page) -> None:
    """Log what the page offered, so a failure is diagnosable from the report."""
    try:
        buttons = []
        for item in page.get_by_role("button").all()[:8]:
            label = (item.inner_text() or "").strip().replace(chr(10), " ")
            if label:
                buttons.append(label[:24])
        fields = page.eval_on_selector_all("input", "els => els.map(e => e.name)")
        log(f"page {page.url.split('?')[0]} offered buttons={buttons} inputs={fields}")
    except Exception as exc:
        log(f"could not read the page: {type(exc).__name__}")


# Instagram hands a sessionid cookie to a browser it merely remembers, so its
# presence is not authentication. Measured here: the profile held one while the
# site kept showing the account behind a "Continue" button, and the jar written
# from it authenticated nothing - gallery-dl got 400 from the media API with it.
# So the question is asked of the site, using an endpoint that answers only for
# a session that is really signed in.
_APP_ID = "936619743392459"


def _logged_in(page) -> bool:
    if not any(c["name"] == "sessionid" and c["value"]
               for c in page.context.cookies()):
        return False
    try:
        result = page.evaluate(
            """async (appId) => {
                const r = await fetch(
                    '/api/v1/users/web_profile_info/?username=instagram',
                    {headers: {'X-IG-App-ID': appId}, credentials: 'include'});
                return {status: r.status, body: (await r.text()).slice(0, 200)};
            }""",
            _APP_ID,
        )
    except Exception:
        return False
    return result.get("status") == 200 and '"user"' in (result.get("body") or "")


_CODE_FIELDS = (
    'input[name="verificationCode"]',
    'input[name="security_code"]',
    'input[autocomplete="one-time-code"]',
    'input[type="tel"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
)


def _fill_code(page, code: str) -> bool:
    """Type the verification code into whichever box the flow is showing."""
    for selector in _CODE_FIELDS:
        box = page.locator(selector).first
        try:
            if box.count() and box.is_visible():
                box.fill(code)
                page.keyboard.press("Enter")
                return True
        except Exception:
            continue
    return False


# Instagram does not always serve a form. To a browser it remembers - and the
# profile is kept between runs precisely so it is remembered - it serves the
# saved account and a button, with no input on the page at all. Measured: the
# login page carried the account name, two "Continue" buttons and zero inputs,
# so waiting for a password box timed out after 45s every time while the way in
# was sitting there. This is the good case, not an edge case: it needs no
# password and raises no checkpoint.
_CONTINUE_LABELS = ("Continue", "Продолжить")
_ANOTHER_LABELS = ("Use another profile", "Log into another account",
                   "Войти в другой аккаунт", "Использовать другой профиль")


def _click_named(page, labels, timeout: int = 4000) -> bool:
    for label in labels:
        try:
            button = page.get_by_role("button", name=label)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _one_tap(page, password: str) -> bool:
    """Take the saved-account route if this is that page. True if signed in.

    Instagram serves a remembered browser the account and a button rather than
    a form, and the profile is kept between runs precisely to be remembered - so
    this is the ordinary case, not an edge one. Measured: the page carried the
    handle, two "Continue" buttons and no inputs at all, so waiting for a
    password box timed out after 45s every time while the way in sat there.

    Clicking it does not sign in by itself. It reveals a password field named
    "pass" for that account, which is what actually completes the login.
    """
    try:
        body = page.inner_text("body")[:600]
    except Exception:
        return False

    handle = os.getenv("IG_HANDLE", "").strip()
    if handle and handle not in body:
        log(f"the saved account on the page is not {handle}; using the form")
        return False
    if not _click_named(page, _CONTINUE_LABELS):
        return False
    log("Instagram offered the saved account; continuing as it")
    page.wait_for_timeout(4000)

    if _logged_in(page):
        return True

    box = _first_visible(page, _PASS_FIELDS)
    if box is None:
        log("the saved account asked for no password and did not sign in")
        return False
    box.fill(password)
    box.press("Enter")
    for _ in range(9):
        page.wait_for_timeout(3000)
        if _logged_in(page):
            return True
    return False


# How long to sit on a checkpoint before giving up. The approval kind is the
# slow one: it waits on somebody tapping a notification somewhere else.
_CHECKPOINT_WAIT = int(os.getenv("IG_CHECKPOINT_WAIT", "300"))
_CONFIRM_LABELS = ("This Was Me", "Это я", "Continue", "Продолжить", "Dismiss")


def _clear_checkpoint(page, since: float) -> None:
    """Answer whatever Instagram put between the password and the session.

    There is more than one kind and which one appears is not ours to choose.
    Measured here: an approval prompt, which asks another signed-in device to
    confirm the login and can only be waited on; and a code by mail, which can
    be read and typed. A button that merely confirms is clicked when offered.
    """
    deadline = time.time() + _CHECKPOINT_WAIT
    said_waiting = False
    tried_code = False

    while time.time() < deadline:
        if _logged_in(page):
            return

        try:
            body = page.inner_text("body")[:800].lower()
        except Exception:
            body = ""

        for label in _CONFIRM_LABELS:
            try:
                button = page.get_by_role("button", name=label)
                if button.count():
                    button.first.click()
                    log(f"clicked {label!r}")
                    page.wait_for_timeout(6000)
                    break
            except Exception:
                continue

        if "approve" in body or "notification" in body or "\u043e\u0434\u043e\u0431\u0440" in body:
            if not said_waiting:
                log("Instagram asked another signed-in device to approve this "
                    "login; waiting for that")
                said_waiting = True
            page.wait_for_timeout(5000)
            continue

        if not tried_code and _first_visible(page, _CODE_FIELDS) is not None:
            tried_code = True
            code = code_from_mail(since)
            if code and _fill_code(page, code):
                log("code entered")
                page.wait_for_timeout(9000)
                continue
            log("could not answer the code prompt")

        page.wait_for_timeout(5000)

    log(f"checkpoint not cleared within {_CHECKPOINT_WAIT}s")


def run() -> int:
    username = os.getenv("IG_USERNAME", "")
    password = os.getenv("IG_PASSWORD", "")
    if not (username and password):
        log("IG_USERNAME/IG_PASSWORD are not set — nothing to do")
        return 2

    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        context = driver.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=True,
            args=[
                "--no-sandbox",
                # Instagram looks for the automation flag; without this the
                # login form is served but the submit quietly does nothing.
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://www.instagram.com/", timeout=60000)
            page.wait_for_timeout(3000)

            if _logged_in(page):
                log("the stored profile is still signed in")
            else:
                log("signing in")
                page.goto(
                    "https://www.instagram.com/accounts/login/", timeout=60000
                )
                page.wait_for_timeout(4000)

                started = time.time() - 60
                if _one_tap(page, password):
                    log("signed in via the saved account")
                else:
                    try:
                        page.wait_for_selector(
                            ", ".join(_PASS_FIELDS), timeout=45000
                        )
                    except Exception:
                        # Say what the page was rather than only that a selector
                        # never appeared - that is the one thing a timeout does
                        # not tell you, and it cost a day here.
                        _describe(page)
                        return 1
                    user_box = _first_visible(page, _USER_FIELDS)
                    pass_box = _first_visible(page, _PASS_FIELDS)
                    if user_box is None or pass_box is None:
                        log("the login form is not the shape this knows")
                        _describe(page)
                        return 1
                    user_box.fill(username)
                    pass_box.fill(password)
                    pass_box.press("Enter")
                    page.wait_for_timeout(9000)

                if not _logged_in(page):
                    _clear_checkpoint(page, started)

            if not _logged_in(page):
                where = page.url
                log(f"not signed in; stopped at {where.split('?')[0]}")
                return 1

            written = write_cookies(context.cookies())
            if not written:
                log("signed in but no cookies to store")
                return 1
            log(f"signed in, {written} cookie(s) written to the jar")
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    code = 1
    try:
        code = run()
    except Exception as exc:
        log(f"failed: {type(exc).__name__}: {exc}")
    report(code == 0)
    sys.exit(code)
