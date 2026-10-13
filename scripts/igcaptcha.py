"""Bounded reCAPTCHA v2 audio handler for the account owner's login browser."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 3 * 1024 * 1024
MAX_ATTEMPTS = 2
MAX_SECONDS = 75


def trusted_audio(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if parts.scheme == "https" and parts.hostname == "www.facebook.com":
            return (
                parts.port in (None, 443)
                and parts.username is None
                and parts.password is None
                and parts.path == "/captcha/tfbaudio/"
            )
        return (
            parts.scheme == "https"
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and parts.hostname
            in ("www.google.com", "www.recaptcha.net", "recaptcha.net")
            and parts.path.startswith("/recaptcha/")
        )
    except ValueError:
        return False


def _frame(page, kind: str):
    for frame in page.frames:
        parts = urlsplit(frame.url)
        if parts.hostname not in (
            "www.google.com",
            "www.recaptcha.net",
            "recaptcha.net",
        ):
            continue
        if not parts.path.startswith("/recaptcha/") or not parts.path.endswith(
            "/" + kind
        ):
            continue
        try:
            if frame.frame_element().is_visible():
                return frame
        except Exception:  # noqa: BLE001,S112 -- frame can detach during navigation
            continue
    return None


def present(page) -> bool:
    from ig_native_captcha import present as native_present

    return (
        native_present(page)
        or _frame(page, "anchor") is not None
        or _frame(page, "bframe") is not None
    )


def _checked(page) -> bool:
    anchor = _frame(page, "anchor")
    if anchor is None:
        return False
    return (
        anchor.locator("#recaptcha-anchor").get_attribute("aria-checked", timeout=2000)
        == "true"
    )


def _audio(frame, url: str) -> bytes:
    if not trusted_audio(url):
        raise ValueError("untrusted_audio")
    # Stream in the provider's frame, using its browser session. Abort before
    # allocating a large response, and never log the signed audio URL.
    encoded = frame.evaluate(
        """async ({url, limit}) => {
        const response = await fetch(url, {signal: AbortSignal.timeout(12000)});
        if (!response.ok || !response.body) throw new Error('audio_unavailable');
        if (Number(response.headers.get('content-length')) > limit)
            throw new Error('audio_too_large');
        const reader = response.body.getReader();
        const chunks = []; let size = 0;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            size += value.length;
            if (size > limit) { await reader.cancel(); throw new Error('audio_too_large'); }
            chunks.push(value);
        }
        let binary = '';
        for (const chunk of chunks) {
            for (let i = 0; i < chunk.length; i += 8192)
                binary += String.fromCharCode(...chunk.subarray(i, i + 8192));
        }
        return btoa(binary);
    }""",
        {"url": url, "limit": MAX_BYTES},
    )
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("audio_too_large")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("audio_unavailable")
    return data


def _transcribe(data: bytes, timeout: float) -> str:
    with tempfile.TemporaryDirectory(prefix="ig-captcha-") as directory:
        path = Path(directory) / "challenge.audio"
        path.write_bytes(data)
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("ig_audio_transcribe.py")),
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=min(45, timeout),
            check=False,
        )
    if result.returncode:
        raise ValueError("local_transcription_unavailable")
    answer = json.loads(result.stdout).get("text", "").strip()
    if not answer or len(answer) > 400:
        raise ValueError("invalid_transcription")
    return answer


def solve(page, log, *, deadline: float | None = None) -> bool:
    """Try at most two audio clips. No visual/identity challenge fallback."""
    from ig_captcha_budget import reserve_attempt

    stop = min(deadline or float("inf"), time.monotonic() + MAX_SECONDS)
    try:
        if _checked(page):
            return True
        if not reserve_attempt():
            log("CAPTCHA automatic attempt deferred by persistent cooldown")
            return False
        from ig_native_captcha import present as native_present, solve as native_solve

        if native_present(page):
            return native_solve(page, log, stop)
        anchor = _frame(page, "anchor")
        if anchor is not None:
            anchor.locator("#recaptcha-anchor").click(timeout=3000)
            page.wait_for_timeout(1500)
        for attempt in range(MAX_ATTEMPTS):
            if stop - time.monotonic() < 5:
                break
            challenge = _frame(page, "bframe")
            if challenge is None:
                return _checked(page)
            button = challenge.locator("#recaptcha-audio-button")
            if button.count() and button.is_visible():
                button.click(timeout=3000)
                page.wait_for_timeout(1000)
            source = challenge.locator("#audio-source")
            if not source.count():
                log("CAPTCHA audio unavailable; automatic attempt stopped")
                return False
            data = _audio(challenge, source.get_attribute("src", timeout=3000) or "")
            remaining = stop - time.monotonic()
            if remaining < 3:
                break
            answer = _transcribe(data, remaining - 2)
            challenge.locator("#audio-response").fill(answer, timeout=3000)
            challenge.locator("#recaptcha-verify-button").click(timeout=3000)
            page.wait_for_timeout(1500)
            if _checked(page):
                log("CAPTCHA audio verified")
                return True
            if attempt + 1 < MAX_ATTEMPTS and stop - time.monotonic() > 5:
                challenge.locator("#recaptcha-reload-button").click(timeout=3000)
                page.wait_for_timeout(1000)
        log("CAPTCHA audio not verified within attempt budget")
    except Exception as exc:  # noqa: BLE001 -- redact browser URLs at this boundary
        # Playwright errors can contain signed URLs; only emit the class name.
        log(f"CAPTCHA automatic attempt stopped ({type(exc).__name__})")
    return False
