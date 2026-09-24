"""Resolve an Instagram post to its media URLs, using the browser.

This exists because the other two roads can both be closed at once, and were.

The embed page needs no account and is tried first, but Instagram serves it with
`"contextJSON":null` for most posts now - measured: one payload across six posts
on the same afternoon. The session path covers the rest, until the account
itself is restricted, at which point every API call is answered with the home
page instead of JSON and gallery-dl and yt-dlp both come back empty.

A browser is a third thing. It renders the post page, and the page carries the
post in its own script tags - Instagram's v1 shape, `carousel_media`,
`image_versions2`, `video_versions`. That is the payload the page was given
rather than a guess at which picture on the screen is the subject, and the
difference matters: scraping `<img>` out of the article returned six to eight
images for posts holding one or two, and no video at all for a reel.

Measured on five posts of known shape: a photo post, a reel, a two-photo
carousel, another photo post, and a deleted one as the control. Five for five -
right counts, right owners, the reel a video rather than its cover frame, and
nothing at all for the deleted one.

It runs beside the login because it needs the same browser profile, and it is
asked for the same way the login is: a file appears, this answers, nothing gets
the docker socket.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REQUEST_FILE = Path(os.getenv("IG_RESOLVE_REQUEST", "/app/data/igresolve_request"))
RESULT_FILE = Path(os.getenv("IG_RESOLVE_RESULT", "/app/data/igresolve_result.json"))
PROFILE_DIR = Path(os.getenv("IG_PROFILE_DIR", "/app/data/ig-profile"))
PAGE_WAIT = int(os.getenv("IG_RESOLVE_WAIT", "9000"))
NAV_TIMEOUT = 60000

# Walk the page's JSON for the node that IS this post, then read the v1 media
# shape off it. Nothing here infers from layout.
_EXTRACT = """
(code) => {
    const out = {items: [], owner: null, caption: null, found: false};

    const isMedia = (o) =>
        o && typeof o === 'object' &&
        (o.code === code || o.shortcode === code) &&
        (o.image_versions2 || o.carousel_media || o.video_versions);

    let target = null;
    for (const s of document.querySelectorAll('script')) {
        const text = s.textContent || '';
        if (!text.includes(code) || text.length < 200) continue;
        let data;
        try { data = JSON.parse(text); } catch (e) { continue; }
        const stack = [data];
        while (stack.length) {
            const cur = stack.pop();
            if (Array.isArray(cur)) { stack.push(...cur); continue; }
            if (cur && typeof cur === 'object') {
                if (isMedia(cur)) { target = cur; break; }
                stack.push(...Object.values(cur));
            }
        }
        if (target) break;
    }
    if (!target) return out;
    out.found = true;

    const best = (candidates) => {
        let pick = null;
        for (const c of candidates || []) {
            if (!c || !c.url) continue;
            if (!pick || (c.width || 0) > (pick.width || 0)) pick = c;
        }
        return pick ? pick.url : null;
    };

    const one = (m) => {
        if (m.video_versions && m.video_versions.length) {
            const v = best(m.video_versions);
            // A video whose URL was not given must never become its cover
            // frame: that looks like success and is not.
            out.items.push({url: v, is_video: true});
            return;
        }
        const img = best((m.image_versions2 || {}).candidates);
        if (img) out.items.push({url: img, is_video: false});
    };

    if (target.carousel_media && target.carousel_media.length) {
        for (const m of target.carousel_media) one(m);
    } else {
        one(target);
    }

    out.owner = (target.user || {}).username || null;
    const cap = target.caption;
    out.caption = cap && cap.text ? cap.text.slice(0, 400) : null;
    return out;
}
"""


def log(message: str) -> None:
    print(f"[igresolve] {message}", flush=True)


def _shortcode(url: str) -> str | None:
    import re

    found = re.search(
        r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url
    )
    return found.group(1) if found else None


def _challenged(page) -> bool:
    """Whether Instagram put a challenge in front of the page instead of it."""
    where = page.url or ""
    return "/auth_platform/" in where or "/challenge/" in where


def _dismiss(page) -> None:
    """Whatever one-button page is in the way, including the scraping warning."""
    for label in ("Dismiss", "Not now", "Не сейчас", "Закрыть"):
        try:
            button = page.get_by_role("button", name=label)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=4000)
                page.wait_for_timeout(3000)
                return
        except Exception:
            continue


def resolve(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    code = _shortcode(url)
    if not code:
        return {"ok": False, "error": "not an instagram post link"}
    if Path(os.getenv("IG_REMOTE_ACTIVE", "/app/data/igremote.active")).exists():
        # A person has the browser open to answer a CAPTCHA. Launching a second
        # Chromium on the same profile would fail on its lock - or worse, win it.
        return {"ok": False, "error": "the browser is in use for a captcha"}

    sys.path.insert(0, "/app/scripts")
    from iglogin_browser import clear_stale_profile_lock

    clear_stale_profile_lock(PROFILE_DIR)
    with sync_playwright() as driver:
        context = driver.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(f"https://www.instagram.com/p/{code}/", timeout=NAV_TIMEOUT)
            page.wait_for_timeout(PAGE_WAIT)
            if _challenged(page):
                # Measured 23 Sep: every post, including ones other roads could
                # still fetch, redirected here. No page load will get past it,
                # and nothing here should try - it is for a person.
                return {"ok": False, "captcha": True,
                        "error": "instagram is asking this account for a captcha"}
            data = page.evaluate(_EXTRACT, code)
            if not data.get("found"):
                # One retry behind whatever interstitial appeared, then give up
                # rather than reporting something about the post that is really
                # about the page in front of it.
                _dismiss(page)
                page.goto(f"https://www.instagram.com/p/{code}/", timeout=NAV_TIMEOUT)
                page.wait_for_timeout(PAGE_WAIT)
                data = page.evaluate(_EXTRACT, code)

            items = [i for i in (data.get("items") or []) if i.get("url")]
            withheld = len(data.get("items") or []) - len(items)
            if withheld:
                # All or nothing: half a carousel is not the post either.
                return {"ok": False, "error": "instagram withheld part of the post"}
            if not items:
                return {"ok": False, "error": "no media on the page"}
            return {
                "ok": True,
                "items": items,
                "owner": data.get("owner"),
                "caption": data.get("caption"),
            }
        finally:
            context.close()


def _answer(payload: dict) -> None:
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(payload), encoding="utf-8")
    try:
        os.chmod(RESULT_FILE, 0o666)
    except OSError:
        pass


def main() -> int:
    if len(sys.argv) > 1:
        # Handy for measuring by hand; the loop below is what the bot uses.
        print(json.dumps(resolve(sys.argv[1]), indent=2)[:1200])
        return 0

    log(f"waiting for a request at {REQUEST_FILE}")
    while True:
        if REQUEST_FILE.exists():
            try:
                url = REQUEST_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                url = ""
            try:
                REQUEST_FILE.unlink()
            except OSError:
                pass
            if url:
                log(f"resolving {url[:90]}")
                started = time.time()
                try:
                    payload = resolve(url)
                except Exception as exc:
                    payload = {"ok": False,
                               "error": f"{type(exc).__name__}: {exc}"[:200]}
                payload["seconds"] = round(time.time() - started, 1)
                _answer(payload)
                log(f"answered: ok={payload.get('ok')} "
                    f"items={len(payload.get('items') or [])} "
                    f"in {payload['seconds']}s")
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
