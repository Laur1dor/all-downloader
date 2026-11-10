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
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROFILE_DIR = Path(os.getenv("IG_PROFILE_DIR", "/app/data/ig-profile"))
COOKIE_FILE = Path(os.getenv("IG_COOKIE_FILE", "/app/data/cookies.txt"))
RESULT_FILE = Path(os.getenv("IG_RESULT_FILE", "/app/data/iglogin_result.txt"))
BLOCKED_FILE = RESULT_FILE.with_name("ig_account_blocked.json")

_CODE_RE = re.compile(r"\b(\d{6})\b")
_CODE_WAIT_SECONDS = int(os.getenv("IG_CODE_WAIT", "180"))
_CODE_POLL_SECONDS = 10

_lines: list[str] = []


def log(message: str) -> None:
    print(f"[iglogin] {message}", flush=True)
    _lines.append(message)


def report(ok: bool) -> None:
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = ("OK" if ok else "FAIL") + "\n" + "\n".join(_lines[-15:])
    _atomic_write(RESULT_FILE, body)


def _atomic_write(path: Path, text: str, *, shared: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(text)
    try:
        os.chmod(temporary, 0o666 if shared else 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _mail_code(raw: bytes, since: float, recipient: str, handle: str) -> str | None:
    """Only a fresh verification for this mailbox and Instagram profile."""
    message = email.message_from_bytes(raw)
    sender = email.utils.parseaddr(message.get("From", ""))[1].lower()
    domain = sender.rpartition("@")[2]
    if domain not in ("instagram.com", "mail.instagram.com", "facebookmail.com", "account.meta.com"):
        return None
    addresses = {address.lower() for _, address in email.utils.getaddresses(message.get_all("To", []) + message.get_all("Delivered-To", []))}
    if recipient.lower() not in addresses:
        return None
    try:
        stamp = email.utils.parsedate_to_datetime(message.get("Date", ""))
        if stamp.tzinfo is None or not since <= stamp.timestamp() <= time.time() + 300:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    body = "\n".join((part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace") for part in message.walk() if part.get_content_type() in ("text/plain", "text/html"))
    if handle and not re.search(r"(?<![a-z0-9_.])" + re.escape(handle) + r"(?![a-z0-9_.])", body, re.I):
        return None
    return _code_from(raw)


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
            with imaplib.IMAP4_SSL(host, timeout=20) as box:
                box.login(user, password)
                box.select("INBOX", readonly=True)
                day = time.strftime("%d-%b-%Y", time.gmtime(since))
                status, data = box.search(None, f'(SINCE "{day}")')
                if status == "OK" and data and data[0]:
                    for num in reversed(data[0].split()[-40:]):
                        status, payload = box.fetch(num, "(BODY.PEEK[])")
                        if status != "OK" or not payload or not payload[0]:
                            continue
                        raw = payload[0][1]
                        code = _mail_code(raw, since, user, os.getenv("IG_HANDLE", ""))
                        if code:
                            log("code found in the mailbox")
                            return code
        except Exception as exc:
            log(f"mailbox read failed: {type(exc).__name__}; check mailbox credentials")
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
    _atomic_write(COOKIE_FILE,
        "\n".join(["# Netscape HTTP Cookie File", *kept, *rows]) + "\n",
        shared=True,
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
def _visible_authenticated_profile(page) -> bool | None:
    """Positive UI evidence only; an inconclusive endpoint never means logout."""
    from urllib.parse import urlsplit
    handle = os.getenv("IG_HANDLE", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", handle) or not isinstance(page.url, str):
        return None
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "www.instagram.com" or url.path != "/":
        return None
    try:
        if (page.locator(f'a[href="/{handle}/"] img').count() > 0
                and page.locator('a[href="/direct/inbox/"]').count() > 0
                and _first_visible(page, _PASS_FIELDS) is None):
            return True
    except Exception:
        pass
    return None


def _authentication_state(page) -> bool | None:
    """Whether this browser is signed in, asked of Instagram rather than assumed.

    The cookie alone proves nothing: one was present here while the site kept
    showing the account behind a "Continue" button, and the jar written from it
    authenticated nothing - gallery-dl got 400 from the media API with it.
    /data/shared_data/ names the viewer when there is one.

    The parsing happens inside the page on purpose. An earlier version pulled
    the body out and sliced it to 4000 characters first; the answer is about
    38 KB, so every parse got half a JSON document and failed, and this returned
    False even with the feed on the screen. A login that worked was written off
    as failed, its cookies were dropped, and the watchdog asked for another one
    - which is the loop that got the account flagged for automation.
    """
    if not any(c["name"] == "sessionid" and c["value"]
               for c in page.context.cookies()):
        return False
    try:
        result = page.evaluate(
            """async () => {
                const r = await fetch('/data/shared_data/',
                    {credentials: 'include', signal: AbortSignal.timeout(15000)});
                if (r.status !== 200) return {status: r.status, user: null};
                let data = null;
                try { data = await r.json(); }
                catch (e) { return {status: r.status, unknown: true}; }
                if (!data.config || !('viewer' in data.config))
                    return {status: r.status, unknown: true};
                const viewer = (data.config || {}).viewer;
                return {status: r.status,
                        user: viewer && viewer.username ? viewer.username : null};
            }"""
        )
    except Exception:
        return _visible_authenticated_profile(page)
    if not isinstance(result, dict) or result.get("unknown"):
        return _visible_authenticated_profile(page)
    if result.get("status") != 200:
        return _visible_authenticated_profile(page)
    return bool(result.get("user"))


def _logged_in(page) -> bool:
    return _authentication_state(page) is True


def _restricted(page) -> bool:
    from urllib.parse import urlsplit

    if urlsplit(page.url).path not in ("/accounts/suspended/", "/accounts/disabled/"):
        return False
    human = page.get_by_text("Confirm you're human", exact=True)
    if human.count() and human.first.is_visible():
        return False
    body = page.inner_text("body", timeout=3000).lower()
    if not any(message in body for message in (
        "we suspended your account", "your account has been disabled",
        "we disabled your account",
    )):
        return False
    log("Instagram account suspended; automatic login paused until account recovery")
    _atomic_write(BLOCKED_FILE, json.dumps({"reason": "suspended", "detected_at": time.time()}))
    return True


def _needs_phone(page) -> bool:
    phone = page.locator('input#phone[type="tel"]')
    if phone.count() and phone.first.is_visible():
        log("Instagram requires a mobile number and SMS/WhatsApp confirmation; email code is not applicable")
        _atomic_write(BLOCKED_FILE, json.dumps({"reason": "phone_required", "detected_at": time.time()}))
        return True
    return False


_CODE_FIELDS = (
    'input[name="verificationCode"]',
    'input[name="security_code"]',
    'input[autocomplete="one-time-code"]',
    'input[type="tel"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
)


def _checkpoint_code_fields(page) -> tuple[str, ...]:
    from urllib.parse import urlsplit
    if urlsplit(page.url).path.rstrip("/") == "/auth_platform/codeentry":
        return (*_CODE_FIELDS, 'input[name="email"]')
    return _CODE_FIELDS


def _fill_code(page, code: str) -> bool:
    """Type the verification code into whichever box the flow is showing."""
    for selector in _checkpoint_code_fields(page):
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
# Pages that stand between a correct password and a session. Each is one button
# and no inputs, so a flow looking for a form finds nothing to do and gives up.
# Measured: /accounts/scraping_warning/ ("We suspect automated behavior on your
# account") offers only Dismiss, and pressing it lands on the feed, signed in.
_INTERSTITIAL_LABELS = ("Dismiss", "Not now", "Не сейчас", "OK", "Закрыть",
                        "Continue", "Продолжить")
_SETTLE_ROUNDS = 6
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


def _settle(page, password: str) -> bool:
    """Walk whatever Instagram puts between here and a session. True if signed in.

    There is no single shape to expect any more, so this does not expect one. It
    looks at what the page offers - a password box, a one-button interstitial -
    deals with that, and asks again. It stops the moment the session answers.
    """
    from igcaptcha import present

    submitted = False
    for _ in range(_SETTLE_ROUNDS):
        if _restricted(page):
            return False
        if present(page):
            return False
        state = _authentication_state(page)
        if state is None:
            log("authentication check inconclusive; no further password submission")
            return False
        if state:
            return True

        box = _first_visible(page, _PASS_FIELDS)
        if box is not None:
            if submitted:
                log("login form still present after one submission; stopping")
                return False
            user_box = _first_visible(page, _USER_FIELDS)
            if user_box is not None:
                user_box.fill(os.getenv("IG_USERNAME", ""))
            box.fill(password)
            box.press("Enter")
            submitted = True
            page.wait_for_timeout(9000)
            continue

        if _click_named(page, _INTERSTITIAL_LABELS):
            page.wait_for_timeout(6000)
            continue

        # Nothing offered and not signed in: say what the page was, so the next
        # failure is diagnosable rather than just a timeout.
        _describe(page)
        return False

    return _logged_in(page)


def _one_tap(page, password: str) -> bool:
    """Take the saved-account route when Instagram offers it.

    It serves a remembered browser the account and a button rather than a form,
    and the profile is kept between runs precisely to be remembered - so this is
    the ordinary case. Clicking it does not sign in by itself; it reveals a
    password field for that account, and may put a warning page in front first.
    """
    try:
        body = page.inner_text("body")[:600]
    except Exception:
        return False

    handle = os.getenv("IG_HANDLE", "").strip()
    # The handle is on the saved-account screen, but not on an interstitial, so
    # its absence is only a reason to refuse when this IS that screen.
    offers_account = _first_visible(page, _PASS_FIELDS) is None and any(
        label in body for label in _CONTINUE_LABELS
    )
    if handle and offers_account and handle not in body:
        log(f"the saved account on the page is not {handle}; using the form")
        return False
    if offers_account:
        if not _click_named(page, _CONTINUE_LABELS):
            return False
        log("Instagram offered the saved account; continuing as it")
        page.wait_for_timeout(4000)

    return _settle(page, password)


# How long to sit on a checkpoint before giving up. The approval kind is the
# slow one: it waits on somebody tapping a notification somewhere else.
_CHECKPOINT_WAIT = int(os.getenv("IG_CHECKPOINT_WAIT", "300"))
_CONFIRM_LABELS = ("This Was Me", "Это я", "Continue", "Продолжить", "Dismiss")


def _dismiss_save_login(page) -> None:
    from urllib.parse import urlsplit
    if not isinstance(page.url, str):
        return
    url = urlsplit(page.url)
    if (url.scheme == "https" and url.hostname == "www.instagram.com"
            and url.path == "/accounts/onetap/"
            and _click_named(page, ("Not now", "Не сейчас"))):
        log("dismissed save-login prompt after authentication")
        page.wait_for_timeout(3000)


def _clear_checkpoint(page, since: float) -> None:
    """Answer whatever Instagram put between the password and the session.

    There is more than one kind and which one appears is not ours to choose.
    Measured here: an approval prompt, which asks another signed-in device to
    confirm the login and can only be waited on; and a code by mail, which can
    be read and typed. A button that merely confirms is clicked when offered.
    """
    from igcaptcha import present, solve

    deadline = time.monotonic() + _CHECKPOINT_WAIT
    said_waiting = False
    tried_code = False
    tried_captcha = False

    while time.monotonic() < deadline:
        _dismiss_save_login(page)
        if _restricted(page):
            return
        if _needs_phone(page):
            return
        state = _authentication_state(page)
        if state:
            return
        if present(page):
            if tried_captcha:
                log("CAPTCHA still present after bounded automatic attempt")
                return
            tried_captcha = True
            if not solve(page, log, deadline=deadline):
                return
            _click_named(page, _CONFIRM_LABELS)
            page.wait_for_timeout(3000)
            continue

        if _first_visible(page, _PASS_FIELDS) is not None:
            log("ordinary login form remains; no checkpoint to answer")
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

        if not tried_code and _first_visible(page, _checkpoint_code_fields(page)) is not None:
            tried_code = True
            code = code_from_mail(since)
            if code and _fill_code(page, code):
                log("code entered")
                page.wait_for_timeout(9000)
                continue
            log("could not answer the code prompt")

        page.wait_for_timeout(5000)

    log(f"checkpoint not cleared within {_CHECKPOINT_WAIT}s")


def clear_stale_profile_lock(profile: Path) -> None:
    """Remove Chromium's singleton lock when the browser holding it is gone.

    The lock is a symlink naming host-pid. When the container is recreated while
    a browser is open, the browser dies without removing it, and every launch in
    the new container then sees the profile "in use on another computer" and
    refuses - measured: the lock still named the old container's hostname and
    every launch failed until it was cleared. The lock is removed only when its
    owner is provably not a live process here, so two browsers are never let
    onto one profile.
    """
    import socket

    lock = profile / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return
    host, _, pid = target.rpartition("-")
    if host == socket.gethostname() and pid.isdigit() and Path(f"/proc/{pid}").exists():
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (profile / name).unlink()
        except OSError:
            pass
    log(f"cleared a stale browser lock left by {target}")


def browser_proxy() -> dict[str, str] | None:
    """Use the same dedicated Instagram exit as the bot's routing policy.

    The browser container has no access to the bot's in-memory router, but both
    share data/routing.toml.  An explicit environment override is useful when
    the browser needs a different exit.  Chromium uses SOCKS5 remote DNS.
    """
    import tomllib

    url = os.getenv("IG_BROWSER_PROXY", "").strip()
    if not url:
        try:
            with Path(os.getenv("ROUTING_FILE", "/app/data/routing.toml")).open("rb") as fh:
                url = str(tomllib.load(fh).get("services", {}).get("instagram", ""))
        except (OSError, tomllib.TOMLDecodeError):
            return None
    if url.startswith("socks5h://"):
        url = "socks5://" + url.removeprefix("socks5h://")
    return {"server": url} if url.startswith(("socks5://", "http://", "https://")) else None


def _run_browser(*, check_only: bool = False) -> int:
    username = os.getenv("IG_USERNAME", "")
    password = os.getenv("IG_PASSWORD", "")
    if not (username and password):
        log("IG_USERNAME/IG_PASSWORD are not set — nothing to do")
        return 2

    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    clear_stale_profile_lock(PROFILE_DIR)
    proxy = browser_proxy()
    if proxy:
        log("Instagram browser using configured proxy")
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
            **({"proxy": proxy} if proxy else {}),
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://www.instagram.com/", timeout=60000)
            page.wait_for_timeout(3000)

            if check_only:
                if _logged_in(page):
                    written = write_cookies(context.cookies())
                    log(f"scheduled session check: signed in; {written} cookies refreshed")
                    return 0 if written else 3
                log("scheduled session check inconclusive; no login attempted")
                return 3

            _dismiss_save_login(page)
            if _restricted(page):
                return 4
            if _needs_phone(page):
                return 4

            from igcaptcha import present

            if present(page) or any(part in page.url for part in (
                "/challenge/", "/checkpoint/", "/auth_platform/",
            )):
                log("stored profile has an Instagram checkpoint; handling it before login")
                _clear_checkpoint(page, time.time() - 60)
            auth_state = _authentication_state(page)
            if auth_state is None:
                log("authentication check inconclusive; keeping profile and cookies unchanged")
                return 3
            if auth_state:
                log("the stored profile is still signed in")
            else:
                log("signing in")
                page.goto(
                    "https://www.instagram.com/accounts/login/", timeout=60000
                )
                page.wait_for_timeout(4000)

                started = time.time() - 60
                if _one_tap(page, password):
                    log("signed in")
                else:
                    log("could not get past what Instagram is showing")

                # Only a checkpoint - a mailed code, an approval from another
                # device - is left once the ordinary pages have been walked.
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
            BLOCKED_FILE.unlink(missing_ok=True)
            return 0
        finally:
            context.close()


def run(*, check_only: bool = False) -> int:
    from ig_profile import ProfileBusy, profile_lock

    try:
        with profile_lock(timeout=0 if check_only else 20):
            return _run_browser(check_only=check_only)
    except ProfileBusy:
        log("Instagram browser profile is busy; login deferred")
        return 3


if __name__ == "__main__":
    code = 1
    check_only = sys.argv[1:] == ["--check-only"]
    try:
        code = run(check_only=check_only)
    except Exception as exc:
        log(f"failed: {type(exc).__name__}: {exc}")
    if not check_only:
        report(code == 0)
    sys.exit(code)
