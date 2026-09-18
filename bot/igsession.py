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

    It asks an endpoint that only a signed-in session can answer, rather than
    reading a page. Two earlier versions read pages and both were fooled the
    same way: Instagram remembers a browser and offers the account back on the
    login screen, so the handle appears there too, and it issues a sessionid
    cookie to a browser it merely remembers. Neither the name nor the cookie is
    evidence of being signed in; an answer from the API is.

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

# Ask something only a signed-in session can answer.
#
# Two earlier attempts read a page instead, and both were wrong in the same way.
# /data/shared_data/ names the account - but it names it on the signed-out page
# too, because Instagram remembers the browser and offers the account back as a
# button. A probe that searched for the handle therefore reported a live session
# while the site was showing a login screen, and the jar it blessed held a
# sessionid that authenticates nothing. Presence of that cookie is not
# authentication either: Instagram issues one to a browser it merely remembers.
#
# This endpoint has no such ambiguity. It answers 200 with data to a session
# that is signed in, and 401 require_login to one that is not.
_PROFILE_API = (
    "https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram"
)
# The web app's own id. Without it the endpoint refuses everyone alike, which
# would make the probe measure the header rather than the session.
_APP_ID = "936619743392459"
_HEADERS = {"X-IG-App-ID": _APP_ID, "Referer": "https://www.instagram.com/"}
# Instagram rate-limits by account and says so in the same 401 shape it uses for
# "you are not signed in", require_login included. Read as signed out, that
# would spend a login attempt on every limited minute - and logins are what
# provoke the limit. It is the absence of an answer, so it is UNKNOWN.
_RATE_LIMITED = "wait a few minutes"
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


def _read_answer(status: int, body: str) -> str:
    """What this reply says about the session, and nothing more."""
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return UNKNOWN
        user = ((data.get("data") or {}).get("user")) or {}
        return LIVE if user else UNKNOWN
    if status == 401:
        if _RATE_LIMITED in body.lower():
            return UNKNOWN
        try:
            data = json.loads(body)
        except ValueError:
            return UNKNOWN
        # Instagram saying in so many words that this needs a login.
        return DEAD if data.get("require_login") else UNKNOWN
    # A 429, a 5xx, a redirect: all of them describe the request, not the account.
    return UNKNOWN


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
            headers=_HEADERS, timeout=_PROBE_TIMEOUT,
        )
    except Exception as exc:
        logger.info("Instagram probe could not start: %s", exc)
        return UNKNOWN, None
    try:
        response = session.get(_PROFILE_API)
    except Exception as exc:
        logger.info("Instagram probe did not reach the site: %s", exc)
        return UNKNOWN, None
    finally:
        session.close()

    state = _read_answer(response.status_code, response.text)
    if state == UNKNOWN:
        logger.info(
            "Instagram probe was inconclusive (HTTP %s)", response.status_code
        )
    # The account is named by configuration rather than by this reply, so that
    # nothing here reports a name Instagram did not actually confirm.
    return state, (os.getenv("IG_HANDLE") or None) if state == LIVE else None


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
