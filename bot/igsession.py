"""Notice that the Instagram account has been signed out, and sign back in.

The account has died three times, and each time the same thing happened: nobody
found out from the bot. Users reported that Instagram "stopped working", and the
session had to be renewed by hand. The bot had every chance to know — it just
never asked.

The trap here is inferring it from downloads. A post can fail because it is
private, deleted, gated behind a follow, geo-blocked, or because the exit died;
reading "the session is dead" out of those is the mistake that already shipped
once, when a live post was declared deleted on a guess. So this does not infer.
It asks Instagram who it is signed in as.

    /data/shared_data/ answers JSON naming the signed-in account, and JSON with
    a null viewer when nobody is signed in. Measured against a genuinely
    authenticated session and two controls - a corrupted sessionid and a
    removed one - it separates them cleanly.

    What took three attempts to see: Instagram remembers a browser. It offers
    the account back on the login screen and hands that browser a sessionid
    cookie, so a jar can hold one and a page can show the handle while nothing
    is signed in. Neither is evidence. Only the served JSON is.

The probe has three answers, not two, and the third is what keeps it safe. When
the request itself cannot be made — no network, no exit, Instagram unreachable —
the answer is "unknown" and nothing happens. Only a reply that Instagram itself
served, saying nobody is signed in, counts as signed out.

Being wrong in the remaining direction is cheap by construction: a needless
login attempt costs one browser run and changes nothing else. No user is told
anything, and no download path is cut short. That asymmetry is deliberate, and
it is why this is allowed to act on its own where the embed reader was not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from html import escape as html_escape
from pathlib import Path

logger = logging.getLogger(__name__)

# Ask /data/shared_data/, and read only the JSON it answers with.
#
# This took three tries, and the reason is worth keeping. Instagram remembers a
# browser: on the login screen it offers the account back behind a "Continue"
# button, and it hands that browser a sessionid cookie. So a jar can hold a
# sessionid, and a page can show the handle, while nothing is signed in at all.
# Every earlier measurement here was taken against exactly that state believing
# it was a live session - which is how one version searched the body for the
# handle and read the login screen as proof of being signed in.
#
# Measured against a genuinely authenticated session for the first time, with
# two controls:
#
#   authenticated      200, JSON, config.viewer.username set, viewerId set
#   corrupt sessionid  200, JSON, viewer null
#   sessionid removed  200, JSON, viewer null
#
# The signed-out answer is JSON that carries viewerId and a null viewer - an
# answer Instagram served, not an absence - so that and only that is DEAD.
# Anything else, including the HTML this returns to a half-remembered browser,
# is UNKNOWN.
#
# web_profile_info was tried as the probe and dropped: it answered 429 to the
# authenticated session while answering 401 to the controls, so it measured the
# rate limit rather than the session, and a watchdog reading it would be blind
# exactly when it mattered.
_SHARED_DATA = "https://www.instagram.com/data/shared_data/"
_PROBE_TIMEOUT = 25

# Answers of the probe. UNKNOWN is not a failure - it is the absence of an
# answer, and it must never be treated as one.
LIVE = "live"
DEAD = "dead"
UNKNOWN = "unknown"

# How often to ask when nothing is prompting it. The session dies on Instagram's
# schedule rather than ours, so this only bounds how long a dead one goes
# unnoticed; fifteen minutes costs one request an hour and change.
_INTERVAL = 900
# A login takes minutes and may end up waiting on a code, so attempts are spaced
# out and back off when they do not work. The last value is the resting rate:
# once a human is needed, asking every two hours is enough to catch the moment
# they fix it, without filling the log.
_BACKOFF = (0, 600, 1800, 3600, 7200)
_LOGIN_TIMEOUT = 420
# A ceiling on logins per day, independent of the backoff.
#
# Measured the hard way: a bug in the signed-in check reported every successful
# login as failed, so this asked for another, and another. Instagram answered
# with "we suspect automated behavior on your account" and began serving HTML to
# the API calls the downloader needs - an account-level restriction that no code
# here can work around. The backoff alone did not bound that, because each
# attempt looked like a fresh first failure.
#
# Four is generous for a session that normally lasts days. What it rules out is
# the loop, and it does so without needing to know why the loop is happening.
_LOGINS_PER_DAY = int(os.getenv("IG_LOGINS_PER_DAY", "4"))
_DAY_SECONDS = 24 * 3600


def _read_answer(status: int, body: str) -> tuple[str, str | None]:
    """What this reply says about the session, and nothing more."""
    if status != 200:
        return UNKNOWN, None
    if body.lstrip()[:1] != "{":
        # The rendered page, which this serves to a browser it half-remembers.
        # It names the account whether or not anybody is signed in, so it is
        # not evidence either way.
        return UNKNOWN, None
    try:
        config = (json.loads(body) or {}).get("config")
    except ValueError:
        return UNKNOWN, None
    if not isinstance(config, dict):
        return UNKNOWN, None

    viewer = config.get("viewer")
    if isinstance(viewer, dict):
        name = viewer.get("username")
        if isinstance(name, str) and name.strip():
            return LIVE, name.strip()
    if viewer is None and "viewerId" in config:
        return DEAD, None
    return UNKNOWN, None


def _probe_sync(cookies_file: Path, proxy: str | None) -> tuple[str, str | None]:
    """(state, username). Never raises: a failed request is UNKNOWN, not DEAD."""
    import http.cookiejar

    from curl_cffi import requests as cffi_requests

    try:
        jar = http.cookiejar.MozillaCookieJar(str(cookies_file))
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = {c.name: c.value for c in jar if "instagram" in c.domain}
    except Exception as exc:
        # A jar that cannot be read is a real problem, but it is ours and not
        # Instagram's, and logging in again would not fix it.
        logger.warning("Instagram cookie jar unreadable: %s", exc)
        return UNKNOWN, None
    if not cookies.get("sessionid"):
        # Nothing to be signed in with. That is not a guess about Instagram.
        return DEAD, None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        session = cffi_requests.Session(
            impersonate="chrome131", proxies=proxies, cookies=cookies,
            timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:
        logger.info("Instagram probe could not start: %s", exc)
        return UNKNOWN, None
    try:
        response = session.get(_SHARED_DATA)
    except Exception as exc:
        logger.info("Instagram probe did not reach the site: %s", exc)
        return UNKNOWN, None
    finally:
        session.close()

    state, name = _read_answer(response.status_code, response.text)
    if state == UNKNOWN:
        logger.info(
            "Instagram probe was inconclusive (HTTP %s)", response.status_code
        )
    return state, name


class InstagramSession:
    """Watches the account and asks the browser container to sign in again.

    The login itself runs in the other container, asked for by dropping a file,
    exactly as /iglogin already does — the bot does not get the docker socket
    for this, because handing it one would hand it root on the host.
    """

    def __init__(
        self,
        cookies_file: Path | None,
        request_file: Path,
        result_file: Path,
        notify=None,
        *,
        enabled: bool = True,
    ) -> None:
        self._cookies = cookies_file
        self._request = request_file
        self._result = result_file
        self._notify = notify
        self.enabled = bool(enabled and cookies_file is not None)
        self._task: asyncio.Task | None = None
        # Only one login at a time, and only one probe: a burst of failed
        # Instagram downloads must not turn into a burst of logins.
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._failures = 0
        self._next_attempt = 0.0
        # Monotonic stamps of the logins asked for, newest last.
        self._logins: list[float] = []
        self._state = UNKNOWN
        self._told_admin_dead = False

    # --- the outside world -------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Instagram session watch is off (no cookie jar)")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def login_now(self) -> tuple[bool, str]:
        """Sign in on demand, sharing the watch's lock. (ok, what it said)

        /iglogin comes through here rather than writing the request file itself.
        Both used to drop the same file and delete the same result file before
        waiting, so whichever finished second could consume - or destroy - the
        answer meant for the first, and the operator would be told a login timed
        out while it had actually worked.
        """
        async with self._lock:
            ok, report = await self._ask_for_login()
            if not ok:
                return False, report
            from bot.proxy import proxy_for

            state, name = await asyncio.to_thread(
                _probe_sync, self._cookies, proxy_for("instagram")
            ) if self._cookies is not None else (UNKNOWN, None)
            if state == LIVE:
                await self._went_live(name)
            return ok, report

    def nudge(self) -> None:
        """Something Instagram-shaped failed; look sooner than the next tick.

        This is a reason to ask, never an answer. The probe still decides.
        """
        if self.enabled:
            self._wake.set()

    @property
    def state(self) -> str:
        return self._state

    # --- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        # A first look at startup, so a session that died while the bot was down
        # is found now rather than in a quarter of an hour.
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the watch die
                logger.exception("Instagram session check failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=_INTERVAL)
            except asyncio.TimeoutError:
                pass

    async def check(self) -> str:
        """Probe, and sign in again if Instagram says nobody is signed in."""
        if not self.enabled or self._cookies is None:
            return UNKNOWN
        if self._lock.locked():
            # A login is already running; its own probe will settle the state.
            return self._state

        async with self._lock:
            from bot.proxy import proxy_for

            state, name = await asyncio.to_thread(
                _probe_sync, self._cookies, proxy_for("instagram")
            )
            if state == UNKNOWN:
                # Say nothing and change nothing: this is the answer that means
                # the question could not be asked.
                return self._state
            if state == LIVE:
                await self._went_live(name)
                return LIVE

            self._state = DEAD
            await self._sign_in_again()
            return self._state

    # --- recovery ----------------------------------------------------------

    async def _went_live(self, name: str | None) -> None:
        recovered = self._state == DEAD or self._told_admin_dead
        if self._state != LIVE:
            # Once per start and once per recovery, so the log says which
            # account the bot is actually working as - and stays quiet after.
            logger.info("Instagram session is live (%s)", name or "unknown")
        self._state = LIVE
        self._failures = 0
        self._next_attempt = 0.0
        if recovered:
            self._told_admin_dead = False
            # Deliberately does not say who fixed it. The operator may have
            # run /iglogin by hand after the warning, and claiming the credit
            # for that is how both messages stop being worth reading.
            await self._say(
                "✅ Instagram снова в строю"
                + (f" — вход как {html_escape(name)}." if name else ".")
            )

    async def _sign_in_again(self) -> None:
        now = time.monotonic()
        if now < self._next_attempt:
            waiting = int((self._next_attempt - now) / 60)
            logger.info("Instagram signed out; next attempt in ~%d min", waiting)
            return

        self._logins = [t for t in self._logins if now - t < _DAY_SECONDS]
        if len(self._logins) >= _LOGINS_PER_DAY:
            # Deliberately quiet: the operator has already been told once that a
            # login is needed, and repeating it every quarter of an hour would
            # be the same loop wearing different clothes.
            logger.warning(
                "Instagram signed out, but %d logins already today - not asking "
                "again until one ages out", len(self._logins)
            )
            return
        self._logins.append(now)

        logger.warning("Instagram is signed out; asking for a login")
        ok, report = await self._ask_for_login()

        confirmed = UNKNOWN
        if ok:
            # Believe the login only as far as the probe does.
            from bot.proxy import proxy_for

            confirmed, name = await asyncio.to_thread(
                _probe_sync, self._cookies, proxy_for("instagram")
            )
            if confirmed == LIVE:
                await self._went_live(name)
                return

        # Space the next attempt out either way. Without this a run of failed
        # Instagram downloads would nudge its way into a loop of logins.
        self._failures += 1
        delay = _BACKOFF[min(self._failures, len(_BACKOFF) - 1)]
        self._next_attempt = time.monotonic() + delay

        if ok and confirmed == UNKNOWN:
            # The login said it worked and the confirming probe could not be
            # taken - which is not the same as the login having failed, and the
            # two go out over different routes, so one saying nothing carries no
            # news about the other. Saying "the login did not work" here would
            # be the module's own rule broken against the operator: an answer
            # invented where there was none. Wait for the next probe instead.
            logger.info("Login reported success; could not confirm it yet")
            return

        if self._told_admin_dead:
            return
        # Escaped: this is the login container's own output, and a Playwright
        # error routinely quotes the element it tripped over. Sent raw into an
        # HTML-parsed message it is rejected by Telegram, and the one warning
        # an outage ever gets would be the one that never arrives.
        detail = html_escape((report or "нет ответа")[:300])
        self._told_admin_dead = await self._say(
            "⚠️ Instagram разлогинился, и автоматический вход не прошёл." + chr(10) + chr(10)
            + f"Что ответил вход: <code>{detail}</code>" + chr(10) + chr(10)
            + "Скорее всего Instagram просит подтвердить вход с устройства, "
            "где вы уже вошли. Подтвердите — бот войдёт сам на следующей "
            "попытке. Либо запустите /iglogin вручную."
        )

    async def _ask_for_login(self) -> tuple[bool, str]:
        """Drop the request file and wait for the container's verdict."""
        try:
            if self._result.exists():
                self._result.unlink()
        except OSError:
            pass
        try:
            self._request.parent.mkdir(parents=True, exist_ok=True)
            self._request.write_text(str(int(time.time())), encoding="utf-8")
        except OSError as exc:
            return False, f"не удалось попросить вход: {exc}"

        deadline = time.monotonic() + _LOGIN_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            if not self._result.exists():
                continue
            try:
                report = self._result.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            head, _, body = report.partition("\n")
            return head.strip() == "OK", (body.strip() or head.strip())
        return False, "контейнер iglogin не ответил — он запущен?"

    async def _say(self, text: str) -> bool:
        """True only if the message actually went out.

        The caller uses this to decide whether it has told anybody. A send that
        Telegram rejected is not a warning delivered, and treating it as one is
        how an outage goes silent: the flag says "already told them" while the
        operator has heard nothing.
        """
        if self._notify is None:
            return False
        try:
            await self._notify(text)
            return True
        except Exception:
            logger.exception("Could not tell the admin about Instagram")
            return False
